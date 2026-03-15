"""
Connection engine — the core of Plaidify.

Loads a connector (Python class or JSON blueprint) for the requested site
and executes the login + extraction flow.

Phase 1: Uses Playwright browser automation via the Browser Pool,
Step Executor, and Data Extractor. Falls back to V1 stub for
Python connectors.
"""

import json
import importlib.util
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Type

from src.config import get_settings
from src.core.connector_base import BaseConnector
from src.core.blueprint import BlueprintV2, load_blueprint
from src.core.browser_pool import get_browser_pool, BrowserPool
from src.core.step_executor import StepExecutor
from src.core.data_extractor import DataExtractor
from src.core.mfa_manager import get_mfa_manager, MFAManager
from src.exceptions import (
    AuthenticationError,
    BlueprintNotFoundError,
    BlueprintValidationError,
    CaptchaRequiredError,
    ConnectionFailedError,
    DataExtractionError,
    MFARequiredError,
    SiteUnavailableError,
)
from src.logging_config import get_logger

logger = get_logger("engine")
settings = get_settings()


async def connect_to_site(
    site: str,
    username: str,
    password: str,
    extract_fields: Optional[list[str]] = None,
    proxy: Optional[dict] = None,
    session_id: Optional[str] = None,
) -> dict:
    """
    Establish a connection to the provided site using the given credentials.

    Tries to use a Python connector first, otherwise loads the JSON blueprint
    and executes it via Playwright.

    Args:
        site: The site identifier (must match a blueprint or connector filename).
        username: The user's username for the target site.
        password: The user's password for the target site.
        extract_fields: Optional list of specific fields to extract (None = all).
        proxy: Optional proxy config for the browser.
        session_id: Optional session ID (generated if not provided).

    Returns:
        dict with 'status' and 'data' keys.

    Raises:
        BlueprintNotFoundError: If no connector or blueprint exists for the site.
        ConnectionFailedError: If the connection attempt fails.
        MFARequiredError: If MFA is needed (client should call /mfa/submit).
    """
    logger.info("Initiating connection", extra={"extra_data": {"site": site}})

    connectors_dir = str(Path(settings.connectors_dir).resolve())

    # ── Try Python connector first ────────────────────────────────────────────
    python_connectors = load_python_connectors(connectors_dir)
    connector_key = f"{site}_connector"
    if connector_key in python_connectors:
        ConnectorClass = python_connectors[connector_key]
        connector_instance = ConnectorClass()
        logger.info(
            "Using Python connector",
            extra={"extra_data": {"site": site, "connector": connector_key}},
        )
        try:
            return connector_instance.connect(username, password)
        except Exception as e:
            logger.error(
                "Python connector failed",
                extra={"extra_data": {"site": site, "error": str(e)}},
            )
            raise ConnectionFailedError(site=site, detail=str(e)) from e

    # ── Load Blueprint ────────────────────────────────────────────────────────
    blueprint = _load_site_blueprint(site, connectors_dir)

    # ── Execute via Playwright ────────────────────────────────────────────────
    return await _execute_blueprint(
        blueprint=blueprint,
        site=site,
        username=username,
        password=password,
        extract_fields=extract_fields,
        proxy=proxy,
        session_id=session_id or str(uuid.uuid4()),
    )


async def submit_mfa_code(session_id: str, code: str) -> dict:
    """
    Submit an MFA code for a pending session.

    The engine waiting on this session will resume and complete the flow.

    Args:
        session_id: The session awaiting MFA.
        code: The MFA code from the user.

    Returns:
        dict with status.
    """
    mfa_manager = get_mfa_manager()
    success = await mfa_manager.submit_code(session_id, code)

    if not success:
        return {
            "status": "error",
            "error": "MFA session not found or expired.",
        }

    return {
        "status": "mfa_submitted",
        "message": "Code submitted. The connection will resume.",
    }


# ── Internal Helpers ──────────────────────────────────────────────────────────


def _load_site_blueprint(site: str, connectors_dir: str) -> BlueprintV2:
    """Load and validate a blueprint for the given site."""
    blueprint_path = Path(connectors_dir) / f"{site}.json"

    if not blueprint_path.exists():
        logger.error(
            "Blueprint not found",
            extra={"extra_data": {"site": site, "path": str(blueprint_path)}},
        )
        raise BlueprintNotFoundError(site=site)

    try:
        return load_blueprint(blueprint_path)
    except json.JSONDecodeError as e:
        raise BlueprintValidationError(site=site, detail=f"Invalid JSON: {e}") from e
    except Exception as e:
        raise BlueprintValidationError(site=site, detail=str(e)) from e


async def _execute_blueprint(
    blueprint: BlueprintV2,
    site: str,
    username: str,
    password: str,
    extract_fields: Optional[list[str]],
    proxy: Optional[dict],
    session_id: str,
) -> dict:
    """
    Execute a V2 blueprint using Playwright.

    Flow:
    1. Acquire a browser context from the pool
    2. Run auth steps (login)
    3. Detect MFA (if configured)
    4. Extract data
    5. Run cleanup steps (logout)
    6. Release the browser context
    """
    pool = await get_browser_pool()
    pooled = await pool.acquire(session_id, proxy=proxy)
    page = None

    try:
        page = await pooled.context.new_page()
        variables = {"username": username, "password": password}
        executor = StepExecutor(page, variables)

        # ── Step 1: Authentication ────────────────────────────────────────────
        logger.info(
            "Executing auth steps",
            extra={"extra_data": {"site": site, "steps": len(blueprint.auth.steps)}},
        )
        await executor.execute_steps(blueprint.auth.steps, context="auth")

        # ── Step 2: MFA Detection ────────────────────────────────────────────
        if blueprint.mfa:
            mfa_result = await _handle_mfa(page, blueprint, site, session_id)
            if mfa_result:
                return mfa_result

        # ── Step 3: Data Extraction ───────────────────────────────────────────
        extraction_defs = blueprint.extract
        if extract_fields:
            extraction_defs = {
                k: v for k, v in extraction_defs.items() if k in extract_fields
            }

        extracted_data: Dict[str, Any] = {}
        if extraction_defs:
            extractor = DataExtractor(page)
            extracted_data = await extractor.extract(extraction_defs, site=site)

        # ── Step 4: Cleanup (logout) ──────────────────────────────────────────
        if blueprint.cleanup:
            try:
                await executor.execute_steps(blueprint.cleanup, context="cleanup")
            except Exception as e:
                logger.warning(
                    "Cleanup steps failed (non-fatal)",
                    extra={"extra_data": {"site": site, "error": str(e)}},
                )

        logger.info(
            "Connection successful",
            extra={"extra_data": {"site": site, "fields_extracted": len(extracted_data)}},
        )

        return {
            "status": "connected",
            "data": extracted_data,
        }

    except MFARequiredError:
        raise
    except (AuthenticationError, ConnectionFailedError, SiteUnavailableError,
            DataExtractionError, BlueprintValidationError, CaptchaRequiredError):
        raise
    except Exception as e:
        logger.error(
            "Unexpected engine error",
            extra={"extra_data": {"site": site, "error": str(e)}},
        )
        raise ConnectionFailedError(site=site, detail=str(e)) from e
    finally:
        # Always close the page and release the context
        if page:
            try:
                await page.close()
            except Exception:
                pass
        await pool.release(session_id)


async def _handle_mfa(
    page,
    blueprint: BlueprintV2,
    site: str,
    session_id: str,
) -> Optional[dict]:
    """
    Check for MFA and handle it.

    If MFA is detected, creates an MFA session and raises MFARequiredError
    (which the API translates to a 403 with session_id for the client).

    For push MFA, polls the page for changes.

    Returns:
        None if no MFA is needed, or a dict response if push MFA completes.
    """
    from playwright.async_api import TimeoutError as PlaywrightTimeout

    mfa_config = blueprint.mfa
    if not mfa_config:
        return None

    try:
        await page.wait_for_selector(
            mfa_config.detection.selector,
            timeout=mfa_config.detection.timeout,
            state="visible",
        )
    except PlaywrightTimeout:
        # No MFA detected — continue
        return None

    logger.info(
        "MFA detected",
        extra={"extra_data": {"site": site, "type": mfa_config.type.value}},
    )

    mfa_manager = get_mfa_manager()
    metadata: Dict[str, Any] = {"mfa_type": mfa_config.type.value}

    # For security questions, extract the question text
    if mfa_config.type.value == "security_question" and mfa_config.question_selector:
        try:
            question_el = await page.query_selector(mfa_config.question_selector)
            if question_el:
                metadata["question"] = await question_el.inner_text()
        except Exception:
            pass

    # Handle push MFA (poll for page change)
    if mfa_config.type.value == "push":
        return await _handle_push_mfa(page, mfa_config, site)

    # For OTP / email code / security question: pause and wait for user input
    session = await mfa_manager.create_session(
        session_id=session_id,
        site=site,
        mfa_type=mfa_config.type.value,
        metadata=metadata,
    )

    # Wait for the user to submit their code
    code = await session.wait_for_code()

    if not code:
        await mfa_manager.remove_session(session_id)
        raise MFARequiredError(
            site=site,
            mfa_type=mfa_config.type.value,
            session_id=session_id,
        )

    # Enter the MFA code
    if mfa_config.input_selector:
        await page.fill(mfa_config.input_selector, code)
        if mfa_config.submit_selector:
            await page.click(mfa_config.submit_selector)
            await page.wait_for_load_state("domcontentloaded")

    await mfa_manager.remove_session(session_id)
    return None  # Continue with extraction


async def _handle_push_mfa(page, mfa_config, site: str) -> Optional[dict]:
    """Poll the page for push MFA approval."""
    import asyncio

    poll_interval = (mfa_config.poll_interval or 2000) / 1000
    poll_timeout = (mfa_config.poll_timeout or 60000) / 1000
    start_time = asyncio.get_event_loop().time()

    while asyncio.get_event_loop().time() - start_time < poll_timeout:
        # Check if we've left the MFA page
        try:
            mfa_element = await page.query_selector(mfa_config.detection.selector)
            if not mfa_element:
                # MFA page gone — push was approved
                return None
        except Exception:
            return None

        await asyncio.sleep(poll_interval)

    # Timed out waiting for push approval
    raise MFARequiredError(site=site, mfa_type="push", session_id="")


def load_python_connectors(connectors_dir: str) -> Dict[str, Type[BaseConnector]]:
    """
    Dynamically load all Python connector classes from the connectors directory.

    Scans for files matching *_connector.py and imports classes that
    inherit from BaseConnector.

    Args:
        connectors_dir: Absolute path to the connectors directory.

    Returns:
        Dict mapping module names to connector classes.
    """
    connectors: Dict[str, Type[BaseConnector]] = {}

    if not os.path.isdir(connectors_dir):
        logger.warning(
            "Connectors directory not found",
            extra={"extra_data": {"path": connectors_dir}},
        )
        return connectors

    for file in os.listdir(connectors_dir):
        if not file.endswith("_connector.py"):
            continue

        module_name = file[:-3]
        module_path = os.path.join(connectors_dir, file)

        try:
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                for attr in dir(module):
                    obj = getattr(module, attr)
                    if (
                        isinstance(obj, type)
                        and issubclass(obj, BaseConnector)
                        and obj is not BaseConnector
                    ):
                        connectors[module_name] = obj
                        logger.debug(
                            "Loaded connector",
                            extra={"extra_data": {"name": module_name}},
                        )
        except Exception as e:
            logger.error(
                "Failed to load connector",
                extra={"extra_data": {"file": file, "error": str(e)}},
            )

    return connectors
