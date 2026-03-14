"""
Connection engine — the core of Plaidify.

Loads a connector (Python class or JSON blueprint) for the requested site
and executes the login + extraction flow.

Currently uses stub logic. Phase 1 will replace this with Playwright.
"""

import json
import importlib.util
import os
import sys
from pathlib import Path
from typing import Any, Dict, Type

from src.config import get_settings
from src.core.connector_base import BaseConnector
from src.exceptions import BlueprintNotFoundError, BlueprintValidationError, ConnectionFailedError
from src.logging_config import get_logger

logger = get_logger("engine")
settings = get_settings()


async def connect_to_site(site: str, username: str, password: str) -> dict:
    """
    Establish a connection to the provided site using the given credentials.

    Tries to use a Python connector first, otherwise falls back to JSON blueprint.

    Args:
        site: The site identifier (must match a blueprint or connector filename).
        username: The user's username for the target site.
        password: The user's password for the target site.

    Returns:
        dict with 'status' and 'data' keys.

    Raises:
        BlueprintNotFoundError: If no connector or blueprint exists for the site.
        ConnectionFailedError: If the connection attempt fails.
    """
    logger.info("Initiating connection", extra={"extra_data": {"site": site}})

    connectors_dir = str(Path(settings.connectors_dir).resolve())

    # Try Python connector first
    python_connectors = load_python_connectors(connectors_dir)
    connector_key = f"{site}_connector"
    if connector_key in python_connectors:
        ConnectorClass = python_connectors[connector_key]
        connector_instance = ConnectorClass()
        logger.info("Using Python connector", extra={"extra_data": {"site": site, "connector": connector_key}})
        try:
            return connector_instance.connect(username, password)
        except Exception as e:
            logger.error("Python connector failed", extra={"extra_data": {"site": site, "error": str(e)}})
            raise ConnectionFailedError(site=site, detail=str(e)) from e

    # Fallback: JSON blueprint
    blueprint_path = Path(connectors_dir) / f"{site}.json"
    if not blueprint_path.exists():
        logger.error("Blueprint not found", extra={"extra_data": {"site": site, "path": str(blueprint_path)}})
        raise BlueprintNotFoundError(site=site)

    try:
        with open(blueprint_path, "r") as f:
            blueprint = json.load(f)
    except json.JSONDecodeError as e:
        raise BlueprintValidationError(site=site, detail=f"Invalid JSON: {e}") from e

    login_url = blueprint.get("login_url")
    fields = blueprint.get("fields", {})
    post_login = blueprint.get("post_login", [])

    if not login_url:
        raise BlueprintValidationError(site=site, detail="Missing 'login_url' in blueprint")

    # TODO: Phase 1 — Replace stub with Playwright browser automation
    logger.info(
        "Simulating login (stub engine)",
        extra={"extra_data": {"site": site, "login_url": login_url}},
    )

    # Stub extraction logic
    extracted_data: Dict[str, Any] = {}
    for step in post_login:
        if "extract" in step:
            for key, selector in step["extract"].items():
                if site == "demo_site":
                    stub_values = {
                        "profile_status": "active",
                        "last_synced": "2025-04-17T12:00:00Z",
                    }
                    extracted_data[key] = stub_values.get(key, f"placeholder_for_{key}")
                elif site == "mock_site":
                    stub_values = {
                        "mock_status": "active",
                        "mock_synced": "2025-04-17T12:00:00Z",
                    }
                    extracted_data[key] = stub_values.get(key, f"placeholder_for_{key}")
                else:
                    extracted_data[key] = f"placeholder_for_{key}"

    return {
        "status": "connected",
        "data": extracted_data or {
            "profile_status": "active",
            "last_synced": "2025-04-17T12:00:00Z",
        },
    }


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
        logger.warning("Connectors directory not found", extra={"extra_data": {"path": connectors_dir}})
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
                        logger.debug("Loaded connector", extra={"extra_data": {"name": module_name}})
        except Exception as e:
            logger.error(
                "Failed to load connector",
                extra={"extra_data": {"file": file, "error": str(e)}},
            )

    return connectors
