"""
Blueprint V2 Schema — Pydantic models for Plaidify site blueprints.

Blueprints define how Plaidify authenticates to a website and extracts data.
V2 introduces structured auth steps, MFA detection, typed data extraction,
and rate-limit/health-check metadata.

Schema version: 2.0
"""

from __future__ import annotations

import json
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ── Enums ─────────────────────────────────────────────────────────────────────


class StepAction(str, Enum):
    """Available step actions in a blueprint."""

    GOTO = "goto"
    FILL = "fill"
    CLICK = "click"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    EXTRACT = "extract"
    CONDITIONAL = "conditional"
    SCROLL = "scroll"
    SELECT = "select"
    IFRAME = "iframe"
    WAIT_FOR_NAVIGATION = "wait_for_navigation"
    EXECUTE_JS = "execute_js"


class FieldType(str, Enum):
    """Data types for extracted fields."""

    TEXT = "text"
    CURRENCY = "currency"
    DATE = "date"
    NUMBER = "number"
    EMAIL = "email"
    PHONE = "phone"
    LIST = "list"
    TABLE = "table"
    BOOLEAN = "boolean"


class TransformType(str, Enum):
    """Built-in transform functions for extracted values."""

    STRIP_WHITESPACE = "strip_whitespace"
    STRIP_DOLLAR_SIGN = "strip_dollar_sign"
    PARSE_DATE = "parse_date"
    TO_LOWERCASE = "to_lowercase"
    TO_UPPERCASE = "to_uppercase"
    REGEX_EXTRACT = "regex_extract"
    TO_NUMBER = "to_number"
    TO_CURRENCY = "to_currency"
    STRIP_COMMAS = "strip_commas"


class MFAType(str, Enum):
    """Supported MFA types."""

    OTP_INPUT = "otp_input"
    EMAIL_CODE = "email_code"
    SECURITY_QUESTION = "security_question"
    PUSH = "push"


class MFAHandler(str, Enum):
    """How MFA is handled."""

    USER_PROMPT = "user_prompt"
    AUTO_DETECT = "auto_detect"


class AuthType(str, Enum):
    """Authentication method types."""

    FORM = "form"
    OAUTH = "oauth"
    BASIC = "basic"
    API_KEY = "api_key"


# ── Step Models ───────────────────────────────────────────────────────────────


class BlueprintStep(BaseModel):
    """A single step in an auth or cleanup flow."""

    action: StepAction = Field(..., description="The action to perform.")
    url: Optional[str] = Field(None, description="URL for goto actions.")
    selector: Optional[str] = Field(None, description="CSS selector for the target element.")
    value: Optional[str] = Field(
        None,
        description="Value to fill/select. Supports {{variable}} interpolation.",
    )
    timeout: Optional[int] = Field(
        None,
        description="Timeout in milliseconds for this step.",
    )
    wait_for_navigation: Optional[bool] = Field(
        False,
        description="Wait for navigation to complete after this step.",
    )
    screenshot_name: Optional[str] = Field(
        None,
        description="Name for screenshot (debug only, never stored in prod).",
    )
    script: Optional[str] = Field(
        None,
        description="JavaScript to execute (for execute_js action).",
    )
    condition_selector: Optional[str] = Field(
        None,
        description="Selector to check for conditional branching.",
    )
    then_steps: Optional[List[BlueprintStep]] = Field(
        None,
        description="Steps to execute if condition is met.",
    )
    else_steps: Optional[List[BlueprintStep]] = Field(
        None,
        description="Steps to execute if condition is not met.",
    )
    iframe_selector: Optional[str] = Field(
        None,
        description="CSS selector for iframe to switch into.",
    )
    direction: Optional[str] = Field(
        None,
        description="Scroll direction: 'down', 'up', or 'to_element'.",
    )
    pixels: Optional[int] = Field(
        None,
        description="Number of pixels to scroll.",
    )

    @field_validator("url")
    @classmethod
    def validate_url_for_goto(cls, v: Optional[str], info) -> Optional[str]:
        """URL is required for goto actions."""
        # Validated at execution time since action may not be available during individual field validation
        return v


# ── MFA Models ────────────────────────────────────────────────────────────────


class MFADetection(BaseModel):
    """How to detect that MFA is required."""

    selector: str = Field(..., description="CSS selector that indicates MFA is needed.")
    timeout: int = Field(
        3000,
        description="Milliseconds to wait for MFA detection after login.",
    )


class MFAConfig(BaseModel):
    """MFA configuration for a blueprint."""

    detection: MFADetection = Field(..., description="How to detect MFA prompts.")
    type: MFAType = Field(..., description="Type of MFA expected.")
    handler: MFAHandler = Field(
        MFAHandler.USER_PROMPT,
        description="How MFA should be handled.",
    )
    input_selector: Optional[str] = Field(
        None,
        description="CSS selector for the MFA input field.",
    )
    submit_selector: Optional[str] = Field(
        None,
        description="CSS selector for the MFA submit button.",
    )
    question_selector: Optional[str] = Field(
        None,
        description="CSS selector for security question text.",
    )
    poll_interval: Optional[int] = Field(
        2000,
        description="Polling interval in ms for push MFA.",
    )
    poll_timeout: Optional[int] = Field(
        60000,
        description="Max wait time in ms for push MFA.",
    )


# ── Extraction Models ────────────────────────────────────────────────────────


class ExtractionField(BaseModel):
    """A single field to extract from a page."""

    selector: Optional[str] = Field(
        None,
        description="CSS selector for the data element. Required for V2, optional for V3 llm_adaptive.",
    )
    type: FieldType = Field(FieldType.TEXT, description="Data type of the field.")
    description: Optional[str] = Field(
        None,
        description="Human-readable description of what this field is (used by LLM extraction).",
    )
    transform: Optional[Union[TransformType, str]] = Field(
        None,
        description="Transform to apply to the raw value.",
    )
    sensitive: bool = Field(
        False,
        description="If true, this field is encrypted in transit and never logged.",
    )
    attribute: Optional[str] = Field(
        None,
        description="HTML attribute to extract instead of text content (e.g., 'href', 'value').",
    )
    default: Optional[Any] = Field(
        None,
        description="Default value if selector not found.",
    )
    timeout: Optional[int] = Field(
        None,
        description="Timeout in milliseconds for this field (overrides default).",
    )
    example: Optional[str] = Field(
        None,
        description="Example value for LLM context (e.g., '$1,234.56').",
    )
    fallback_selector: Optional[str] = Field(
        None,
        description="Fallback CSS selector if primary selector fails (V3).",
    )


class ListExtractionField(BaseModel):
    """Configuration for extracting a list of items (e.g., transaction rows)."""

    selector: Optional[str] = Field(
        None,
        description="CSS selector for each row/item. Required for V2, optional for V3 llm_adaptive.",
    )
    type: FieldType = Field(FieldType.LIST, description="Must be 'list' or 'table'.")
    description: Optional[str] = Field(
        None,
        description="Human-readable description of this list (used by LLM extraction).",
    )
    fields: Dict[str, ExtractionField] = Field(
        ...,
        description="Fields to extract from each row.",
    )
    max_items: Optional[int] = Field(
        None,
        description="Maximum number of items to extract.",
    )
    pagination: Optional[PaginationConfig] = Field(
        None,
        description="Pagination configuration for multi-page extraction.",
    )


class PaginationConfig(BaseModel):
    """Configuration for paginated data extraction."""

    next_selector: str = Field(..., description="CSS selector for the 'next page' button.")
    max_pages: int = Field(5, description="Maximum number of pages to traverse.")
    wait_after_click: int = Field(2000, description="ms to wait after clicking next.")


# Fix forward reference
ListExtractionField.model_rebuild()


# ── Rate Limit & Health ──────────────────────────────────────────────────────


class ExtractionStrategy(str, Enum):
    """How data extraction should be performed."""

    SELECTOR = "selector"  # V2: hardcoded CSS selectors
    LLM_ADAPTIVE = "llm_adaptive"  # V3: LLM-based with selector caching


class RateLimitConfig(BaseModel):
    """Rate limiting configuration for the target site."""

    max_requests_per_hour: int = Field(
        10,
        description="Maximum requests per hour to this site.",
    )
    min_interval_seconds: int = Field(
        30,
        description="Minimum interval between requests in seconds.",
    )


class HealthCheckConfig(BaseModel):
    """Health check for the target site."""

    url: str = Field(..., description="URL to check for site availability.")
    expected_status: int = Field(200, description="Expected HTTP status code.")


# ── Auth Config ──────────────────────────────────────────────────────────────


class AuthConfig(BaseModel):
    """Authentication configuration."""

    type: AuthType = Field(AuthType.FORM, description="Authentication method.")
    steps: List[BlueprintStep] = Field(
        ...,
        description="Ordered steps to perform authentication.",
    )


# ── Top-Level Blueprint ──────────────────────────────────────────────────────


class BlueprintV2(BaseModel):
    """
    Blueprint V2/V3 — the complete definition for connecting to a website.

    Defines authentication flow, MFA handling, data extraction, cleanup,
    rate limiting, and health checks.

    V2: All extraction fields must have CSS selectors.
    V3: Adds 'llm_adaptive' strategy — fields use descriptions instead of selectors,
        and the LLM figures out the correct selectors at runtime.
    """

    schema_version: str = Field(
        "2.0",
        description="Blueprint schema version. '2.0' or '3.0'.",
    )
    name: str = Field(..., description="Human-readable site name.")
    domain: str = Field(..., description="Target website domain.")
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization (e.g., 'banking', 'us').",
    )
    auth: AuthConfig = Field(..., description="Authentication configuration.")
    mfa: Optional[MFAConfig] = Field(
        None,
        description="MFA configuration (if the site supports/requires it).",
    )
    extraction_strategy: ExtractionStrategy = Field(
        ExtractionStrategy.SELECTOR,
        description="Extraction approach: 'selector' (V2 CSS) or 'llm_adaptive' (V3 LLM).",
    )
    extract: Dict[str, Union[ExtractionField, ListExtractionField]] = Field(
        default_factory=dict,
        description="Data fields to extract after authentication.",
    )
    page_context: Optional[str] = Field(
        None,
        description="Description of the page for LLM context (V3, e.g. 'utility bill dashboard').",
    )
    fallback_selectors: Optional[Dict[str, str]] = Field(
        None,
        description="Fallback CSS selectors for critical fields when LLM unavailable (V3).",
    )
    cleanup: Optional[List[BlueprintStep]] = Field(
        None,
        description="Steps to execute after extraction (e.g., logout).",
    )
    rate_limit: Optional[RateLimitConfig] = Field(
        None,
        description="Rate limiting configuration.",
    )
    health_check: Optional[HealthCheckConfig] = Field(
        None,
        description="Health check configuration.",
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if v not in ("1.0", "2.0", "3.0"):
            raise ValueError(f"Unsupported schema version: {v}. Expected '1.0', '2.0', or '3.0'.")
        return v

    @property
    def is_llm_adaptive(self) -> bool:
        """Check if this blueprint uses LLM-adaptive extraction."""
        return self.extraction_strategy == ExtractionStrategy.LLM_ADAPTIVE


# ── Legacy V1 Conversion ─────────────────────────────────────────────────────


def convert_v1_to_v2(v1_data: dict) -> BlueprintV2:
    """
    Convert a V1 blueprint (current format) to a V2 BlueprintV2 model.

    V1 format:
        {
            "name": "...",
            "login_url": "...",
            "fields": {"username": "#user", "password": "#pass", "submit": "#login-btn"},
            "post_login": [{"wait": "..."}, {"extract": {...}}]
        }

    Args:
        v1_data: Raw V1 blueprint dictionary.

    Returns:
        BlueprintV2 model instance.
    """
    fields = v1_data.get("fields", {})
    login_url = v1_data.get("login_url", "")
    name = v1_data.get("name", "Unknown Site")

    # Build auth steps from V1 fields
    auth_steps: List[Dict[str, Any]] = []
    auth_steps.append({"action": "goto", "url": login_url})

    username_selector = fields.get("username")
    if username_selector:
        auth_steps.append({"action": "fill", "selector": username_selector, "value": "{{username}}"})

    password_selector = fields.get("password")
    if password_selector:
        auth_steps.append({"action": "fill", "selector": password_selector, "value": "{{password}}"})

    submit_selector = fields.get("submit")
    if submit_selector:
        auth_steps.append({"action": "click", "selector": submit_selector, "wait_for_navigation": True})

    # Build extract and wait steps from post_login
    extract_fields: Dict[str, Any] = {}
    for step in v1_data.get("post_login", []):
        if "wait" in step:
            auth_steps.append({"action": "wait", "selector": step["wait"]})
        if "extract" in step:
            for key, selector in step["extract"].items():
                extract_fields[key] = {"selector": selector, "type": "text"}

    # Derive domain from login_url
    domain = ""
    if login_url:
        from urllib.parse import urlparse
        parsed = urlparse(login_url)
        domain = parsed.netloc or parsed.hostname or ""

    return BlueprintV2(
        schema_version="2.0",
        name=name,
        domain=domain,
        tags=[],
        auth=AuthConfig(type=AuthType.FORM, steps=[BlueprintStep(**s) for s in auth_steps]),
        extract={k: ExtractionField(**v) for k, v in extract_fields.items()},
    )


def load_blueprint(path: Path) -> BlueprintV2:
    """
    Load and parse a blueprint from a JSON file.

    Automatically detects V1 vs V2/V3 format and converts if needed.

    Args:
        path: Path to the blueprint JSON file.

    Returns:
        Validated BlueprintV2 model.

    Raises:
        json.JSONDecodeError: If the file contains invalid JSON.
        pydantic.ValidationError: If the blueprint fails schema validation.
    """
    with open(path, "r") as f:
        data = json.load(f)

    # Detect version
    schema_version = data.get("schema_version", "1.0")

    if schema_version == "1.0" or "login_url" in data:
        return convert_v1_to_v2(data)

    return BlueprintV2.model_validate(data)


def load_blueprint_from_dict(data: dict) -> BlueprintV2:
    """Load and validate a blueprint from a dictionary.

    Identical to load_blueprint() but accepts a dict instead of a file path.

    Args:
        data: Blueprint data as a dictionary.

    Returns:
        Validated BlueprintV2 model.
    """
    schema_version = data.get("schema_version", "1.0")

    if schema_version == "1.0" or "login_url" in data:
        return convert_v1_to_v2(data)

    return BlueprintV2.model_validate(data)
