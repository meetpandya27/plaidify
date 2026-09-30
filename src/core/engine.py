"""
Connection engine — the core of Plaidify.

Loads a connector (Python class or JSON blueprint) for the requested site
and executes the login + extraction flow.

Blueprints run in a pooled Playwright context under the read-only policy:
sign in (recognising rejected credentials), answer MFA through the MFA
manager (with its own time budget), extract, then log out. Python connectors
run in a worker thread under the same time budget.
"""

import asyncio
import importlib.util
import inspect
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type, Union
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeout

from src import metrics
from src.config import get_settings
from src.core.blueprint import (
    BlueprintStep,
    BlueprintV2,
    ExtractionField,
    FieldType,
    ListExtractionField,
    MFAType,
    StepAction,
    TrustTier,
    blueprint_is_executable,
    load_blueprint,
    resolve_trust_tier,
)
from src.core.browser_pool import PooledContext, get_browser_pool
from src.core.connector_base import BaseConnector
from src.core.data_extractor import DataExtractor, parse_number
from src.core.dom_simplifier import DOMSimplifier
from src.core.extraction_prompt import (
    ExtractionPromptBuilder,
    fields_from_blueprint_extract,
    filter_requested,
    validate_selector_map,
)
from src.core.llm_provider import (
    BaseLLMProvider,
    FallbackChain,
    LLMProviderError,
    create_provider,
)
from src.core.mfa_manager import MFARejectedError, MFATimeoutError, get_mfa_manager
from src.core.multimodal_extractor import MultimodalExtractor
from src.core.network_policy import AddressPolicy, navigation_block_reason
from src.core.page_checks import first_present, indicator_present, selector_visible
from src.core.read_only_policy import ExecutionPhase, ReadOnlyExecutionPolicy
from src.core.selector_cache import SelectorCache
from src.core.site_rate_limit import get_site_rate_limiter
from src.core.step_executor import StepExecutor, describe_browser_error
from src.exceptions import (
    AuthenticationError,
    BlueprintNotFoundError,
    BlueprintValidationError,
    ConnectionFailedError,
    DataExtractionError,
    MFARequiredError,
    PlaidifyError,
    ReadOnlyPolicyViolationError,
    SiteUnavailableError,
)
from src.logging_config import get_logger
from src.tracing import span

logger = get_logger("engine")
settings = get_settings()

_SITE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")
# Waiting for the signed-in page (or an error) when the blueprint names no timeout.
_LOGIN_OUTCOME_TIMEOUT_MS = 10_000
# How long a failure-only outcome check looks before assuming success.
_FAILURE_ONLY_WINDOW_MS = 2_000
_CLEANUP_TIMEOUT_SECONDS = 20.0
_CLOCK_POLL_SECONDS = 1.0

# ── Module-Level Singletons ──────────────────────────────────────────────────

_selector_cache: Optional[SelectorCache] = None


def get_selector_cache() -> SelectorCache:
    """Get or create the module-level selector cache singleton."""
    global _selector_cache
    if _selector_cache is None:
        cache_path = os.environ.get("PLAIDIFY_SELECTOR_CACHE_PATH")
        _selector_cache = SelectorCache(persist_path=cache_path)
    return _selector_cache


# ── Time budget ───────────────────────────────────────────────────────────────


class _RunClock:
    """The automation time budget for one connection.

    Time spent waiting on the user — for an MFA code or a push approval — is
    not counted; that wait has its own budget (MFA_TIMEOUT_SECONDS).
    """

    def __init__(self, budget_seconds: float) -> None:
        self.budget = float(budget_seconds)
        self._used = 0.0
        self._started = time.monotonic()
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    def pause(self) -> None:
        if not self._paused:
            self._used += time.monotonic() - self._started
            self._paused = True

    def resume(self) -> None:
        if self._paused:
            self._started = time.monotonic()
            self._paused = False

    def remaining(self) -> float:
        used = self._used if self._paused else self._used + (time.monotonic() - self._started)
        return self.budget - used

    @contextmanager
    def waiting_on_user(self) -> Iterator[None]:
        self.pause()
        try:
            yield
        finally:
            self.resume()


async def _run_within_budget(coro: Any, *, clock: _RunClock, site: str) -> dict:
    """Run ``coro`` and cancel it once the clock's automation budget is spent."""
    task = asyncio.ensure_future(coro)
    try:
        while True:
            if not clock.paused and clock.remaining() <= 0:
                task.cancel()
                try:
                    await task
                except BaseException:  # noqa: BLE001 - the task's own outcome no longer matters
                    pass
                raise ConnectionFailedError(
                    site=site,
                    detail=f"Connection timed out after {int(clock.budget)}s.",
                )
            timeout = _CLOCK_POLL_SECONDS if clock.paused else min(_CLOCK_POLL_SECONDS, max(0.05, clock.remaining()))
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                return task.result()
    finally:
        if not task.done():
            task.cancel()


# ── Entry points ──────────────────────────────────────────────────────────────


async def connect_to_site(
    site: str,
    username: str,
    password: str,
    extract_fields: Optional[list[str]] = None,
    proxy: Optional[dict] = None,
    session_id: Optional[str] = None,
    *,
    interactive_mfa: bool = True,
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
        interactive_mfa: When False (unattended refreshes), an MFA challenge ends
            the run at once with MFARequiredError instead of waiting for a code.

    Returns:
        dict with 'status', 'data', 'extraction_method' and 'metadata' keys.

    Raises:
        BlueprintNotFoundError: If no connector exists for the site, or it may not run here.
        AuthenticationError: If the site rejects the credentials (or the MFA code).
        MFATimeoutError: If the user did not answer the MFA challenge in time.
        MFARequiredError: If MFA is needed and ``interactive_mfa`` is False.
        RateLimitedError: If this account was connected too recently for the site's rate limit.
        ConnectionFailedError: If the connection attempt fails.
    """
    logger.info("Initiating connection", extra={"extra_data": {"site": site}})

    connectors_dir = str(Path(settings.connectors_dir).resolve())
    _validate_site_name(site)

    # ── Try Python connector first ────────────────────────────────────────────
    connector_class = _load_python_connector(connectors_dir, site)
    if connector_class is not None:
        return await _run_python_connector(connector_class, site, username, password)

    # ── Load Blueprint ────────────────────────────────────────────────────────
    blueprint, blueprint_path = _load_site_blueprint(site, connectors_dir)
    if not blueprint_is_executable(
        blueprint.tags,
        demo_mode=settings.demo_mode,
        allow_internal=settings.engine_allow_internal_connectors,
    ):
        # Internal, fixture and sandbox connectors drive the server's browser at
        # local test portals; outside demo mode they do not exist.
        logger.warning(
            "Refusing to run a non-public connector outside demo mode",
            extra={"extra_data": {"site": site}},
        )
        raise BlueprintNotFoundError(site=site)
    trust = resolve_trust_tier(blueprint_path, blueprint, operator_trusted=_operator_trusted_sites())

    await get_site_rate_limiter().acquire(site, username, blueprint.rate_limit)

    # ── Execute via Playwright ────────────────────────────────────────────────
    clock = _RunClock(settings.engine_timeout_seconds)
    return await _run_within_budget(
        _execute_blueprint(
            blueprint=blueprint,
            site=site,
            username=username,
            password=password,
            extract_fields=extract_fields,
            proxy=proxy,
            session_id=session_id or str(uuid.uuid4()),
            trust=trust,
            clock=clock,
            interactive_mfa=interactive_mfa,
        ),
        clock=clock,
        site=site,
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


def _validate_site_name(site: str) -> None:
    if not _SITE_NAME.match(site or ""):
        raise BlueprintValidationError(
            site=site,
            detail="Invalid site name. Only alphanumeric characters, underscores, and hyphens are allowed.",
        )


def _operator_trusted_sites() -> List[str]:
    return [name.strip() for name in (settings.engine_trusted_connectors or "").split(",") if name.strip()]


def _load_site_blueprint(site: str, connectors_dir: str) -> Tuple[BlueprintV2, Path]:
    """Load and validate a blueprint for the given site; returns it with its path."""
    _validate_site_name(site)

    blueprint_path = Path(connectors_dir) / f"{site}.json"

    # Defense in depth: ensure resolved path stays inside connectors_dir
    resolved = blueprint_path.resolve()
    connectors_resolved = Path(connectors_dir).resolve()
    if resolved.parent != connectors_resolved:
        raise BlueprintValidationError(site=site, detail="Invalid site name.")

    if not blueprint_path.exists():
        logger.error(
            "Blueprint not found",
            extra={"extra_data": {"site": site, "path": str(blueprint_path)}},
        )
        raise BlueprintNotFoundError(site=site)

    try:
        return load_blueprint(blueprint_path), resolved
    except json.JSONDecodeError as e:
        raise BlueprintValidationError(site=site, detail=f"Invalid JSON: {e}") from e
    except Exception as e:
        raise BlueprintValidationError(site=site, detail=str(e)) from e


def _address_policy_for(trust: TrustTier) -> AddressPolicy:
    """Which addresses the browser may reach for a blueprint of this trust tier.

    Untrusted blueprints never reach private networks; trusted ones follow
    BROWSER_BLOCK_PRIVATE_NETWORKS. Loopback only in demo mode and tests.
    """
    return AddressPolicy(
        block_private=True if not trust.is_trusted else settings.browser_block_private_networks,
        allow_loopback=settings.demo_mode or settings.engine_allow_internal_connectors,
    )


# ── Python connectors ────────────────────────────────────────────────────────

# path -> (mtime_ns, connector class or None); re-imported only when the file changes.
_PYTHON_CONNECTOR_CACHE: Dict[str, Tuple[int, Optional[Type[BaseConnector]]]] = {}


def _import_connector_file(path: Path) -> Optional[Type[BaseConnector]]:
    """Import one ``*_connector.py`` file (cached by modification time)."""
    key = str(path.resolve())
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return None
    cached = _PYTHON_CONNECTOR_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    module_name = f"plaidify_connectors.{path.stem}"
    connector: Optional[Type[BaseConnector]] = None
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            for attr in dir(module):
                obj = getattr(module, attr)
                if isinstance(obj, type) and issubclass(obj, BaseConnector) and obj is not BaseConnector:
                    connector = obj
                    if getattr(obj, "__module__", None) == module_name:
                        break
    except Exception as e:
        logger.error(
            "Failed to load connector",
            extra={"extra_data": {"file": path.name, "error": str(e)}},
        )
        connector = None

    _PYTHON_CONNECTOR_CACHE[key] = (mtime, connector)
    if connector is not None:
        logger.debug("Loaded connector", extra={"extra_data": {"name": path.stem}})
    return connector


def _load_python_connector(connectors_dir: str, site: str) -> Optional[Type[BaseConnector]]:
    path = Path(connectors_dir) / f"{site}_connector.py"
    if not path.is_file():
        return None
    return _import_connector_file(path)


def load_python_connectors(connectors_dir: str) -> Dict[str, Type[BaseConnector]]:
    """
    Load all Python connector classes from the connectors directory.

    Scans for files matching *_connector.py and imports classes that
    inherit from BaseConnector. Imports are cached and only repeated when a
    file changes.

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

    for file in sorted(os.listdir(connectors_dir)):
        if not file.endswith("_connector.py"):
            continue
        connector = _import_connector_file(Path(connectors_dir) / file)
        if connector is not None:
            connectors[file[:-3]] = connector

    return connectors


async def _run_python_connector(
    connector_class: Type[BaseConnector],
    site: str,
    username: str,
    password: str,
) -> dict:
    """Run a Python connector off the event loop, under the engine's time budget.

    A synchronous ``connect`` runs in a worker thread; when the budget runs
    out the caller gets a timeout, although Python cannot stop the thread
    itself, which finishes (or hangs) on its own.
    """
    if not blueprint_is_executable(
        getattr(connector_class, "tags", None),
        demo_mode=settings.demo_mode,
        allow_internal=settings.engine_allow_internal_connectors,
    ):
        raise BlueprintNotFoundError(site=site)

    logger.info(
        "Using Python connector",
        extra={"extra_data": {"site": site, "connector": connector_class.__name__}},
    )
    budget = settings.engine_timeout_seconds
    try:
        instance = connector_class()
        connect = instance.connect
        if inspect.iscoroutinefunction(connect):
            work = connect(username, password)
        else:
            work = asyncio.to_thread(connect, username, password)
        result = await asyncio.wait_for(work, timeout=budget)
    except asyncio.TimeoutError as e:
        raise ConnectionFailedError(site=site, detail=f"Connector timed out after {budget}s.") from e
    except PlaidifyError:
        raise
    except Exception as e:
        reason = describe_browser_error(e)
        logger.error(
            "Python connector failed",
            extra={"extra_data": {"site": site, "error": reason}},
        )
        raise ConnectionFailedError(site=site, detail=reason) from e

    if not isinstance(result, dict):
        raise ConnectionFailedError(site=site, detail="Connector returned an invalid result.")
    return result


# ── LLM Extraction Pipeline ─────────────────────────────────────────────────


def _create_llm_provider() -> Optional[BaseLLMProvider]:
    """Create an LLM provider from settings. Returns None if not configured."""
    if not settings.llm_api_key:
        return None

    common = dict(
        api_key=settings.llm_api_key,
        base_url=settings.llm_base_url,
        max_tokens=settings.llm_max_tokens,
        temperature=settings.llm_temperature,
        timeout=settings.llm_timeout,
        effort=settings.llm_effort,
    )

    primary = create_provider(settings.llm_provider, model=settings.llm_model, **common)

    if settings.llm_fallback_model:
        fallback = create_provider(settings.llm_provider, model=settings.llm_fallback_model, **common)
        return FallbackChain([primary, fallback])

    return primary


def _get_page_path(page: Any) -> str:
    """Extract the URL path from a Playwright page for cache keying."""
    try:
        parsed = urlparse(page.url)
        return parsed.path or "/"
    except Exception:
        return "/"


def _defs_to_field_defs(
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
) -> List:
    """Convert blueprint extraction defs to LLM FieldDefinition list."""
    raw = {name: fd.model_dump(exclude_none=True) for name, fd in extraction_defs.items()}
    return fields_from_blueprint_extract(raw)


def _is_missing(value: Any, field_def: Union[ExtractionField, ListExtractionField]) -> bool:
    if value is None:
        return True
    if isinstance(field_def, ListExtractionField):
        return not isinstance(value, list) or len(value) == 0
    return isinstance(value, str) and not value.strip()


def _temp_defs_from_selectors(
    selectors: Dict[str, Any],
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
) -> Dict[str, Union[ExtractionField, ListExtractionField]]:
    """Copies of the blueprint's field definitions pointed at ``selectors``."""
    temp_defs: Dict[str, Union[ExtractionField, ListExtractionField]] = {}
    for name, sel_info in selectors.items():
        original = extraction_defs.get(name)
        if isinstance(original, ListExtractionField) and isinstance(sel_info, dict):
            columns = sel_info.get("fields") or {}
            new_fields = {
                col_name: col_def.model_copy(update={"selector": columns[col_name]})
                for col_name, col_def in original.fields.items()
                if isinstance(columns.get(col_name), str)
            }
            if new_fields:
                temp_defs[name] = original.model_copy(update={"selector": sel_info["row"], "fields": new_fields})
        elif isinstance(original, ExtractionField) and isinstance(sel_info, str):
            temp_defs[name] = original.model_copy(update={"selector": sel_info})
    return temp_defs


async def _extract_with_cached_selectors(
    page: Any,
    cached_selectors: Dict[str, Any],
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
    domain: str,
    page_path: str,
    *,
    read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
) -> Optional[Dict[str, Any]]:
    """Try to extract data using cached CSS selectors.

    The cache is used only when it covers every requested field, and a run
    counts as a hit only when every field came back with a value; otherwise
    the entry records a failure (three in a row invalidate it) and None sends
    the caller on to the LLM.
    """
    cache = get_selector_cache()

    selectors = validate_selector_map(cached_selectors, _defs_to_field_defs(extraction_defs))
    if not selectors or set(selectors) != set(extraction_defs):
        return None

    temp_defs = _temp_defs_from_selectors(selectors, extraction_defs)
    if set(temp_defs) != set(extraction_defs):
        return None

    try:
        extractor = DataExtractor(page, read_only_policy=read_only_policy)
        result = await extractor.extract(temp_defs, site=site)
    except Exception as e:
        logger.warning(
            "Cached selector extraction failed",
            extra={"extra_data": {"site": site, "error": str(e).splitlines()[0] if str(e) else type(e).__name__}},
        )
        cache.record_failure(domain, page_path)
        return None

    missing = [name for name, field_def in temp_defs.items() if _is_missing(result.get(name), field_def)]
    if missing:
        logger.warning(
            "Cached selectors found nothing for some fields",
            extra={"extra_data": {"site": site, "fields": missing}},
        )
        cache.record_failure(domain, page_path)
        return None

    cache.record_success(domain, page_path)
    return result


def _normalized_text(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def _numbers_agree(a: Any, b: Any) -> bool:
    first, second = parse_number(a), parse_number(b)
    return first is not None and second is not None and abs(first - second) < 0.005


def _values_agree(page_value: Any, llm_value: Any, field_def: Union[ExtractionField, ListExtractionField]) -> bool:
    """Whether the value re-read from the page matches what the model reported."""
    if llm_value is None:
        return False
    if isinstance(field_def, ListExtractionField):
        if not isinstance(page_value, list) or not isinstance(llm_value, list) or not page_value or not llm_value:
            return False
        first_page, first_llm = page_value[0], llm_value[0]
        if not isinstance(first_llm, dict):
            return False
        shared = [key for key, value in first_llm.items() if value is not None and first_page.get(key) is not None]
        return bool(shared) and all(
            _values_agree(first_page[key], first_llm[key], field_def.fields[key]) for key in shared
        )
    if field_def.type in (FieldType.CURRENCY, FieldType.NUMBER):
        return _numbers_agree(page_value, llm_value)
    if field_def.type == FieldType.BOOLEAN:
        return bool(page_value) == bool(llm_value)
    return _normalized_text(page_value) == _normalized_text(llm_value)


def _grounded(value: Any, page_text: str) -> bool:
    """Whether a model-reported scalar appears verbatim in the page's visible text."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        number = abs(float(value))
        candidates = {f"{number:,.2f}", f"{number:.2f}", f"{number:g}", f"{number:,.0f}" if number.is_integer() else ""}
        return any(candidate and candidate in page_text for candidate in candidates)
    text = _normalized_text(value)
    return bool(text) and text in _normalized_text(page_text)


async def _visible_text(page: Any) -> str:
    try:
        return await page.inner_text("body", timeout=2000)
    except Exception:
        return ""


async def _reread_with_selectors(
    page: Any,
    selectors: Dict[str, Any],
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
    read_only_policy: Optional[ReadOnlyExecutionPolicy],
) -> Dict[str, Any]:
    """Read the fields again from the live page with the model's selectors ({} on failure)."""
    temp_defs = _temp_defs_from_selectors(selectors, extraction_defs)
    if not temp_defs:
        return {}
    try:
        return await DataExtractor(page, read_only_policy=read_only_policy).extract(temp_defs, site=site)
    except Exception as e:
        logger.warning(
            "Re-reading model selectors failed",
            extra={"extra_data": {"site": site, "error": str(e).splitlines()[0] if str(e) else type(e).__name__}},
        )
        return {}


async def _extract_with_llm(
    page: Any,
    blueprint: BlueprintV2,
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
    domain: str,
    page_path: str,
    *,
    read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
) -> Optional[Dict[str, Any]]:
    """Run full LLM extraction: simplify DOM → prompt → LLM → verify on the page → cache selectors.

    The model's answer is not taken on trust. Unrequested keys are dropped,
    and every value is read again from the live page with the selectors the
    model returned; that page value is what comes back. A field whose
    selector finds nothing keeps the model's value only if it appears
    verbatim in the page's visible text. Selectors are cached only when every
    field re-read successfully and agreed with the model.

    Returns extracted data or None if LLM is unavailable / fails.
    """
    provider = _create_llm_provider()
    if provider is None:
        logger.warning("LLM extraction skipped — no API key configured")
        return None

    field_defs = _defs_to_field_defs(extraction_defs)
    try:
        # 1. Simplify DOM
        simplifier = DOMSimplifier(token_budget=settings.llm_token_budget)
        dom_result = await simplifier.simplify(page)

        logger.info(
            "DOM simplified",
            extra={
                "extra_data": {
                    "site": site,
                    "token_estimate": dom_result.token_estimate,
                    "elements": len(dom_result.element_map),
                }
            },
        )

        # 2. Build prompt
        prompt_builder = ExtractionPromptBuilder()
        prompt = prompt_builder.build_extraction_prompt(
            dom_result.html,
            field_defs,
            page_context=blueprint.page_context,
        )

        # 3. Call LLM (structured output where the model supports it)
        response = await provider.extract(
            prompt,
            system_prompt=prompt_builder.system_prompt,
            json_schema=prompt_builder.response_schema(field_defs),
        )

        # 4. Parse response
        result = prompt_builder.parse_response(response)
    except (LLMProviderError, ValueError, TypeError) as e:
        logger.error(
            "LLM extraction failed",
            extra={"extra_data": {"site": site, "error": str(e)}},
        )
        return None
    except Exception as e:
        logger.error(
            "LLM extraction failed",
            extra={"extra_data": {"site": site, "error": describe_browser_error(e)}},
        )
        return None
    finally:
        if provider:
            await provider.close()

    llm_data = filter_requested(result.data, field_defs)
    selectors = validate_selector_map(result.selectors, field_defs)
    page_values = await _reread_with_selectors(page, selectors, extraction_defs, site, read_only_policy)

    final: Dict[str, Any] = {}
    all_verified = bool(selectors) and set(selectors) == set(extraction_defs)
    page_text: Optional[str] = None
    for name, field_def in extraction_defs.items():
        page_value = page_values.get(name)
        llm_value = llm_data.get(name)
        if not _is_missing(page_value, field_def):
            final[name] = page_value
            if not _values_agree(page_value, llm_value, field_def):
                all_verified = False
            continue
        all_verified = False
        if isinstance(field_def, ExtractionField) and llm_value is not None:
            if page_text is None:
                page_text = await _visible_text(page)
            final[name] = llm_value if _grounded(llm_value, page_text) else None
        else:
            final[name] = None

    logger.info(
        "LLM extraction complete",
        extra={
            "extra_data": {
                "site": site,
                "confidence": result.confidence,
                "fields": sum(1 for value in final.values() if value is not None),
                "selectors": len(selectors),
                "verified": all_verified,
            }
        },
    )

    # 5. Cache selectors only when they reproduced the model's answer on the page
    if all_verified and result.confidence >= 0.5:
        cache = get_selector_cache()
        cache.put(domain, page_path, selectors, confidence=result.confidence)

    if all(value is None for value in final.values()):
        return None
    return final


async def _extract_with_multimodal(
    page: Any,
    blueprint: BlueprintV2,
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
) -> Optional[Dict[str, Any]]:
    """Fallback: use multimodal (screenshot) extraction.

    Returns extracted data or None if unavailable / fails.
    """
    provider = _create_llm_provider()
    if provider is None:
        return None

    try:
        field_defs = _defs_to_field_defs(extraction_defs)

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(
            page,
            field_defs,
            page_context=blueprint.page_context,
        )

        logger.info(
            "Multimodal extraction complete",
            extra={
                "extra_data": {
                    "site": site,
                    "confidence": result.confidence,
                    "fields": len(result.data),
                    "screenshot_bytes": result.screenshot_size_bytes,
                }
            },
        )

        if result.confidence >= 0.3:
            return filter_requested(result.data, field_defs) or None

        logger.warning(
            "Multimodal confidence too low",
            extra={"extra_data": {"site": site, "confidence": result.confidence}},
        )
        return None

    except (LLMProviderError, ValueError, TypeError) as e:
        logger.error(
            "Multimodal extraction failed",
            extra={"extra_data": {"site": site, "error": str(e)}},
        )
        return None
    except Exception as e:
        logger.error(
            "Multimodal extraction failed",
            extra={"extra_data": {"site": site, "error": describe_browser_error(e)}},
        )
        return None
    finally:
        if provider:
            await provider.close()


async def _extract_with_fallback_selectors(
    page: Any,
    blueprint: BlueprintV2,
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
    *,
    read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
) -> Optional[Dict[str, Any]]:
    """Last resort: use blueprint fallback_selectors for critical fields.

    Returns partial data or None if no fallback selectors are defined.
    """
    if not blueprint.fallback_selectors:
        return None

    temp_defs: Dict[str, Union[ExtractionField, ListExtractionField]] = {}
    for name, selector in blueprint.fallback_selectors.items():
        if name in extraction_defs and isinstance(extraction_defs[name], ExtractionField):
            temp_defs[name] = extraction_defs[name].model_copy(update={"selector": selector})

    if not temp_defs:
        return None

    try:
        extractor = DataExtractor(page, read_only_policy=read_only_policy)
        data = await extractor.extract(temp_defs, site=site)
    except Exception as e:
        logger.warning(
            "Fallback selector extraction failed",
            extra={"extra_data": {"site": site, "error": str(e).splitlines()[0] if str(e) else type(e).__name__}},
        )
        return None
    return data if any(value is not None for value in data.values()) else None


async def _extract_llm_adaptive(
    page: Any,
    blueprint: BlueprintV2,
    extraction_defs: Dict[str, Union[ExtractionField, ListExtractionField]],
    site: str,
    *,
    read_only_policy: Optional[ReadOnlyExecutionPolicy] = None,
) -> tuple[Dict[str, Any], str]:
    """Full LLM-adaptive extraction pipeline with cascading fallbacks.

    Tries in order:
    1. Cached CSS selectors (fast, no LLM cost)
    2. Full LLM extraction (DOM → prompt → LLM → verified on the page)
    3. Multimodal fallback (screenshot → vision LLM)
    4. Blueprint fallback_selectors (hardcoded last-resort selectors)

    Returns:
        Tuple of (extracted_data, extraction_method).

    Raises:
        DataExtractionError: If all methods fail.
    """
    domain = blueprint.domain
    page_path = _get_page_path(page)

    # 1. Try cached selectors
    cache = get_selector_cache()
    cache_entry = cache.get(domain, page_path)

    if cache_entry:
        logger.info(
            "Trying cached selectors",
            extra={
                "extra_data": {
                    "site": site,
                    "confidence": cache_entry.confidence,
                    "hits": cache_entry.hit_count,
                }
            },
        )
        cached_data = await _extract_with_cached_selectors(
            page,
            cache_entry.selectors,
            extraction_defs,
            site,
            domain,
            page_path,
            read_only_policy=read_only_policy,
        )
        if cached_data:
            return cached_data, "cached_selectors"

    # 2. Full LLM extraction
    logger.info("Running LLM extraction", extra={"extra_data": {"site": site}})
    llm_data = await _extract_with_llm(
        page, blueprint, extraction_defs, site, domain, page_path, read_only_policy=read_only_policy
    )
    if llm_data:
        return llm_data, "llm"

    # 3. Multimodal fallback
    logger.info(
        "Trying multimodal fallback",
        extra={"extra_data": {"site": site}},
    )
    multimodal_data = await _extract_with_multimodal(page, blueprint, extraction_defs, site)
    if multimodal_data:
        return multimodal_data, "multimodal"

    # 4. Fallback selectors
    logger.info(
        "Trying fallback selectors",
        extra={"extra_data": {"site": site}},
    )
    fallback_data = await _extract_with_fallback_selectors(
        page, blueprint, extraction_defs, site, read_only_policy=read_only_policy
    )
    if fallback_data:
        return fallback_data, "fallback_selectors"

    # All methods exhausted
    raise DataExtractionError(
        site=site,
        detail="All extraction methods failed (cached selectors, LLM, multimodal, fallback selectors).",
    )


# ── Blueprint execution ──────────────────────────────────────────────────────


def _policy_violation_from_new_blocks(policy: ReadOnlyExecutionPolicy, since: int) -> Optional[str]:
    """The reason of the newest request/network block since index ``since``, if any."""
    for blocked in reversed(policy.blocked_actions[since:]):
        if blocked.action in {"request", "network", "redirect", "goto"}:
            return blocked.reason
    return None


async def _run_steps(
    executor: StepExecutor,
    steps: List[BlueprintStep],
    context: str,
    policy: ReadOnlyExecutionPolicy,
) -> None:
    """Run steps; a step that failed because the policy refused its request reports that instead."""
    before = len(policy.blocked_actions)
    try:
        await executor.execute_steps(steps, context=context)
    except (ConnectionFailedError, SiteUnavailableError) as exc:
        reason = _policy_violation_from_new_blocks(policy, before)
        if reason:
            raise ReadOnlyPolicyViolationError(reason) from exc
        raise


async def _await_login_outcome(page: Any, blueprint: BlueprintV2, site: str) -> str:
    """Wait for the page to show the login outcome: 'success', 'mfa', or 'unknown' (nothing declared).

    Raises:
        AuthenticationError: the blueprint's failure indicator appeared.
        ConnectionFailedError: a success indicator is declared but neither it
            nor an error appeared in time.
    """
    auth = blueprint.auth
    if auth.success is None and auth.failure is None:
        return "unknown"

    probes = []
    if auth.failure is not None:
        probes.append(("failure", lambda: indicator_present(page, auth.failure)))
    if blueprint.mfa is not None:
        probes.append(("mfa", lambda: selector_visible(page, blueprint.mfa.detection.selector)))
    if auth.success is not None:
        probes.append(("success", lambda: indicator_present(page, auth.success)))
        timeout = auth.success.timeout or _LOGIN_OUTCOME_TIMEOUT_MS
    else:
        timeout = _FAILURE_ONLY_WINDOW_MS

    outcome = await first_present(probes, timeout_ms=timeout)
    if outcome == "failure":
        raise AuthenticationError(site=site)
    if outcome is None:
        if auth.success is not None:
            raise ConnectionFailedError(
                site=site,
                detail="Sign-in did not complete: neither the signed-in page nor a login error appeared.",
            )
        return "unknown"
    return outcome


async def _sign_in(
    page: Any,
    executor: StepExecutor,
    blueprint: BlueprintV2,
    site: str,
    policy: ReadOnlyExecutionPolicy,
) -> str:
    """Run the auth steps and establish the outcome; raises AuthenticationError on rejected credentials."""
    try:
        await _run_steps(executor, blueprint.auth.steps, "auth", policy)
    except (ConnectionFailedError, SiteUnavailableError) as exc:
        # A step that never found the signed-in page: was it the password?
        if executor.credentials_entered and await indicator_present(page, blueprint.auth.failure):
            raise AuthenticationError(site=site) from exc
        raise
    return await _await_login_outcome(page, blueprint, site)


async def _await_mfa_outcome(page: Any, blueprint: BlueprintV2, site: str) -> str:
    """After a code was entered: 'success', 'failure' (code rejected) or 'unknown'."""
    mfa = blueprint.mfa
    assert mfa is not None
    success = mfa.success or blueprint.auth.success
    failure = mfa.failure
    if success is None and failure is None:
        return "unknown"

    probes = []
    if failure is not None:
        probes.append(("failure", lambda: indicator_present(page, failure)))
    if success is not None:
        probes.append(("success", lambda: indicator_present(page, success)))
        timeout = success.timeout or _LOGIN_OUTCOME_TIMEOUT_MS
    else:
        timeout = _FAILURE_ONLY_WINDOW_MS

    outcome = await first_present(probes, timeout_ms=timeout)
    if outcome is not None:
        return outcome
    if success is None:
        return "unknown"
    if await selector_visible(page, mfa.detection.selector):
        return "failure"  # the site is still asking for a code
    raise ConnectionFailedError(
        site=site,
        detail="MFA did not complete: neither the signed-in page nor an error appeared.",
    )


def _internal_step(**fields: Any) -> BlueprintStep:
    """A step the engine builds itself (e.g. typing the MFA code); skips blueprint validation."""
    return BlueprintStep.model_construct(**fields)


async def _submit_mfa_code(
    page: Any,
    executor: StepExecutor,
    blueprint: BlueprintV2,
    code: str,
    policy: ReadOnlyExecutionPolicy,
) -> None:
    """Type the code into the MFA form and submit it, through the same policy as any step."""
    mfa = blueprint.mfa
    assert mfa is not None and mfa.input_selector
    executor.variables["mfa_code"] = code
    try:
        steps = [_internal_step(action=StepAction.FILL, selector=mfa.input_selector, value="{{mfa_code}}")]
        if mfa.submit_selector:
            steps.append(_internal_step(action=StepAction.CLICK, selector=mfa.submit_selector))
        await _run_steps(executor, steps, "mfa", policy)
        if not mfa.submit_selector:
            await page.press(mfa.input_selector, "Enter")
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10000)
        except PlaywrightTimeout:
            pass
    finally:
        executor.variables.pop("mfa_code", None)


async def _handle_mfa(
    page: Any,
    blueprint: BlueprintV2,
    site: str,
    session_id: str,
    *,
    executor: StepExecutor,
    policy: ReadOnlyExecutionPolicy,
    clock: _RunClock,
    interactive: bool = True,
) -> None:
    """
    Detect an MFA challenge and see it through.

    Creates an MFA session the client answers through /mfa/submit, types the
    code in, and checks the site's verdict. A rejected code reopens the
    session for another try (up to MFA_MAX_ATTEMPTS codes). Waiting for the
    user has its own budget and does not count against the automation budget.

    Raises:
        MFATimeoutError: no code (or push approval) arrived in time.
        MFARejectedError: the site rejected the code(s).
        MFARequiredError: MFA appeared on a non-interactive run.
    """
    mfa_config = blueprint.mfa
    if not mfa_config:
        return

    try:
        await page.wait_for_selector(
            mfa_config.detection.selector,
            timeout=mfa_config.detection.timeout,
            state="visible",
        )
    except PlaywrightTimeout:
        # No MFA detected — continue
        return

    mfa_type = mfa_config.type.value
    logger.info(
        "MFA detected",
        extra={"extra_data": {"site": site, "type": mfa_type}},
    )
    metrics.record_mfa_challenge(mfa_type)

    if not interactive:
        raise MFARequiredError(site=site, mfa_type=mfa_type, session_id=session_id)

    metadata: Dict[str, Any] = {"mfa_type": mfa_type}

    # For security questions, extract the question text
    if mfa_config.type == MFAType.SECURITY_QUESTION and mfa_config.question_selector:
        try:
            question_el = await page.query_selector(mfa_config.question_selector)
            if question_el:
                metadata["question"] = await question_el.inner_text()
        except Exception:
            pass

    if mfa_config.type == MFAType.PUSH:
        await _handle_push_mfa(page, blueprint, site, session_id, metadata=metadata, clock=clock)
        return

    mfa_manager = get_mfa_manager()
    budget = settings.mfa_timeout_seconds
    session = await mfa_manager.create_session(
        session_id=session_id,
        site=site,
        mfa_type=mfa_type,
        metadata=metadata,
        ttl=budget,
    )
    deadline = time.monotonic() + budget
    try:
        while True:
            remaining = deadline - time.monotonic()
            code = None
            if remaining > 0:
                with clock.waiting_on_user():
                    code = await session.wait_for_code(timeout=remaining)
            if not code:
                raise MFATimeoutError(site=site, mfa_type=mfa_type, session_id=session_id)

            await _submit_mfa_code(page, executor, blueprint, code, policy)
            if await _await_mfa_outcome(page, blueprint, site) != "failure":
                return

            attempts_left = settings.mfa_max_attempts - session.attempts
            if attempts_left <= 0:
                raise MFARejectedError(site=site, mfa_type=mfa_type, attempts=session.attempts)
            logger.info(
                "MFA code rejected; asking for another",
                extra={"extra_data": {"site": site, "attempts_left": attempts_left}},
            )
            await mfa_manager.reopen_session(
                session_id,
                metadata={"mfa_error": "invalid_code", "attempts_remaining": attempts_left},
            )
    finally:
        await mfa_manager.remove_session(session_id)


async def _mfa_prompt_state(page: Any, selector: str) -> Optional[bool]:
    """Whether the MFA prompt is showing: True / False, or None when the page can't be read right now."""
    try:
        return bool(await page.locator(selector).first.is_visible())
    except Exception:
        return None


async def _handle_push_mfa(
    page: Any,
    blueprint: BlueprintV2,
    site: str,
    session_id: str,
    *,
    metadata: Dict[str, Any],
    clock: _RunClock,
) -> None:
    """Wait for the user to approve a push notification, polling the page.

    The session tells the client to prompt the user; a code submitted for it
    ("I approved it") only makes the engine look again at once. Errors while
    polling — the page is mid-navigation — mean "not yet", never "approved".
    """
    mfa_config = blueprint.mfa
    assert mfa_config is not None
    mfa_manager = get_mfa_manager()
    budget = min(float(settings.mfa_timeout_seconds), (mfa_config.poll_timeout or 60000) / 1000)
    poll_interval = (mfa_config.poll_interval or 2000) / 1000
    success = mfa_config.success or blueprint.auth.success

    session = await mfa_manager.create_session(
        session_id=session_id,
        site=site,
        mfa_type=MFAType.PUSH.value,
        metadata=metadata,
        ttl=int(budget) + 1,
    )
    deadline = time.monotonic() + budget
    prompt_gone = 0
    try:
        with clock.waiting_on_user():
            while True:
                if mfa_config.failure is not None and await indicator_present(page, mfa_config.failure):
                    raise MFARejectedError(site=site, mfa_type=MFAType.PUSH.value)
                if success is not None:
                    if await indicator_present(page, success):
                        return
                else:
                    showing = await _mfa_prompt_state(page, mfa_config.detection.selector)
                    prompt_gone = prompt_gone + 1 if showing is False else 0
                    if prompt_gone >= 2:
                        return  # the prompt went away and stayed away: approved

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MFATimeoutError(site=site, mfa_type=MFAType.PUSH.value, session_id=session_id)
                await session.wait_for_code(timeout=min(poll_interval, remaining))
    finally:
        await mfa_manager.remove_session(session_id)


async def _check_page_origin(page: Any, pooled: PooledContext, policy: ReadOnlyExecutionPolicy) -> None:
    """Before anything is read: the page must be on the blueprint's domains and on public addresses."""
    reason = navigation_block_reason(page.url, policy.host_rules)
    if not reason and pooled.address_policy is not None:
        for frame in page.frames:
            url = frame.url or ""
            if url.startswith(("http://", "https://")):
                reason = await pooled.address_policy.url_block_reason(url)
                if reason:
                    break
    if reason:
        policy.record_blocked("read", reason, target=(page.url or "").split("?", 1)[0])
        raise ReadOnlyPolicyViolationError(reason)


def _raise_on_network_violation(pooled: PooledContext) -> None:
    if pooled.network_violation:
        raise ReadOnlyPolicyViolationError(pooled.network_violation)


async def _run_cleanup(
    executor: StepExecutor,
    blueprint: BlueprintV2,
    policy: ReadOnlyExecutionPolicy,
    site: str,
) -> None:
    """Log out. Never fatal, and bounded in time."""
    policy.set_phase(ExecutionPhase.CLEANUP)
    try:
        await asyncio.wait_for(
            executor.execute_steps(blueprint.cleanup or [], context="cleanup"),
            timeout=_CLEANUP_TIMEOUT_SECONDS,
        )
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(
            "Cleanup steps failed (non-fatal)",
            extra={"extra_data": {"site": site, "error": describe_browser_error(e)}},
        )


async def _execute_blueprint(
    blueprint: BlueprintV2,
    site: str,
    username: str,
    password: str,
    extract_fields: Optional[list[str]],
    proxy: Optional[dict],
    session_id: str,
    *,
    trust: TrustTier = TrustTier.UNTRUSTED,
    clock: Optional[_RunClock] = None,
    interactive_mfa: bool = True,
) -> dict:
    """
    Execute a V2 blueprint using Playwright.

    Flow:
    1. Acquire a browser context from the pool
    2. Run auth steps (login) and check the outcome
    3. Detect and complete MFA (if configured)
    4. Extract data
    5. Run cleanup steps (logout) — whenever the session got signed in
    6. Release the browser context
    """
    clock = clock or _RunClock(settings.engine_timeout_seconds)
    pool = await get_browser_pool()
    read_only_policy = ReadOnlyExecutionPolicy.for_blueprint(blueprint, enabled=settings.strict_read_only_mode)
    pooled = await pool.acquire(
        session_id,
        proxy=proxy,
        read_only_policy=read_only_policy,
        address_policy=_address_policy_for(trust),
    )
    page = None
    executor: Optional[StepExecutor] = None
    signed_in = False

    try:
        page = await pooled.context.new_page()
        variables = {"username": username, "password": password}
        # JavaScript is a trust-tier privilege: only connectors bundled with
        # Plaidify (or vouched for by the operator) get it, and the policy
        # still refuses it once signed in.
        executor = StepExecutor(
            page,
            variables,
            allow_js_execution=trust.allows_javascript,
            read_only_policy=read_only_policy,
            site=site,
            failure_check=blueprint.auth.failure,
            debug_screenshots=settings.debug,
        )

        # ── Step 1: Authentication ────────────────────────────────────────────
        read_only_policy.set_phase(ExecutionPhase.AUTH)
        logger.info(
            "Executing auth steps",
            extra={"extra_data": {"site": site, "steps": len(blueprint.auth.steps)}},
        )
        await _sign_in(page, executor, blueprint, site, read_only_policy)
        executor.failure_check = None  # signed in: later waits are not about the password
        _raise_on_network_violation(pooled)

        # ── Step 2: MFA ───────────────────────────────────────────────────────
        if blueprint.mfa:
            read_only_policy.set_phase(ExecutionPhase.MFA)
            await _handle_mfa(
                page,
                blueprint,
                site,
                session_id,
                executor=executor,
                policy=read_only_policy,
                clock=clock,
                interactive=interactive_mfa,
            )

        signed_in = True
        read_only_policy.set_phase(ExecutionPhase.READ)
        await _check_page_origin(page, pooled, read_only_policy)
        _raise_on_network_violation(pooled)

        # ── Step 3: Data Extraction ───────────────────────────────────────────
        extraction_defs = blueprint.extract
        if extract_fields is not None:
            extraction_defs = {k: v for k, v in extraction_defs.items() if k in extract_fields}

        extracted_data: Dict[str, Any] = {}
        extraction_method = "none"

        if extraction_defs:
            with span("engine.extract", **{"plaidify.site": site}):
                if blueprint.is_llm_adaptive:
                    extracted_data, extraction_method = await _extract_llm_adaptive(
                        page=page,
                        blueprint=blueprint,
                        extraction_defs=extraction_defs,
                        site=site,
                        read_only_policy=read_only_policy,
                    )
                else:
                    extractor = DataExtractor(page, read_only_policy=read_only_policy)
                    extracted_data = await extractor.extract(extraction_defs, site=site)
                    extraction_method = "css_selectors"
        _raise_on_network_violation(pooled)

        missing_fields = sorted(
            name for name, field_def in extraction_defs.items() if _is_missing(extracted_data.get(name), field_def)
        )
        missing_required = [name for name in missing_fields if extraction_defs[name].required]
        if missing_required:
            raise DataExtractionError(site=site, detail=f"Required field(s) not found: {', '.join(missing_required)}.")
        scalar_fields = [name for name, d in extraction_defs.items() if isinstance(d, ExtractionField)]
        if scalar_fields and all(name in missing_fields for name in scalar_fields):
            raise DataExtractionError(site=site, detail="None of the requested fields could be read from the page.")

        logger.info(
            "Connection successful",
            extra={
                "extra_data": {
                    "site": site,
                    "fields_extracted": len(extracted_data) - len(missing_fields),
                    "extraction_method": extraction_method,
                }
            },
        )

        response_metadata: Dict[str, Any] = {}
        if read_only_policy.enabled:
            response_metadata["read_only_policy"] = read_only_policy.to_metadata()
        downloads = await pool.collect_downloads(pooled, max_bytes=settings.browser_max_download_bytes)
        if downloads:
            response_metadata["downloads"] = downloads
        sensitive_fields = [
            name for name in blueprint.sensitive_field_names() if name.split("[]", 1)[0] in extraction_defs
        ]
        if sensitive_fields:
            # Tells storage which values to encrypt or omit; the values themselves are never logged.
            response_metadata["sensitive_fields"] = sensitive_fields
        if missing_fields:
            response_metadata["missing_fields"] = missing_fields

        metrics.record_extraction(site, "success")
        return {
            "status": "connected",
            "data": extracted_data,
            "extraction_method": extraction_method,
            "metadata": response_metadata or None,
        }

    except ReadOnlyPolicyViolationError as e:
        if read_only_policy.enabled:
            e.metadata = {
                "read_only_policy": read_only_policy.to_metadata(),
            }
        raise
    except (AuthenticationError, MFARequiredError, MFATimeoutError):
        raise
    except PlaidifyError as e:
        if pooled.network_violation:
            raise ReadOnlyPolicyViolationError(
                pooled.network_violation, metadata={"read_only_policy": read_only_policy.to_metadata()}
            ) from e
        raise
    except Exception as e:
        if pooled.network_violation:
            raise ReadOnlyPolicyViolationError(
                pooled.network_violation, metadata={"read_only_policy": read_only_policy.to_metadata()}
            ) from e
        metrics.record_extraction(site, "error")
        reason = describe_browser_error(e)
        logger.error(
            "Unexpected engine error",
            extra={"extra_data": {"site": site, "error": reason}},
        )
        raise ConnectionFailedError(site=site, detail=reason) from e
    finally:
        # Log out whenever the session got signed in, whatever happened after.
        if signed_in and blueprint.cleanup and executor is not None and page is not None and not page.is_closed():
            await _run_cleanup(executor, blueprint, read_only_policy, site)
        # Always close the page and release the context
        if page:
            try:
                await page.close()
            except Exception:
                pass
        await pool.release(session_id)
