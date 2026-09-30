"""
Blueprint V2 Schema — Pydantic models for Plaidify site blueprints.

Blueprints define how Plaidify authenticates to a website and extracts data.
V2 introduces structured auth steps, MFA detection, typed data extraction,
and rate-limit/health-check metadata; V3 adds LLM-adaptive extraction.

Every model rejects unknown keys, so a typo fails validation instead of
silently disappearing, and each step action declares the fields it needs.

Schema versions: 2.0, 3.0 (and legacy 1.0 files, converted on load).
"""

from __future__ import annotations

import json
import re
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.network_policy import (
    ALLOWED_SCHEMES,
    HostRule,
    TargetPattern,
    parse_domain_rule,
    parse_target,
    split_url,
)


class _StrictModel(BaseModel):
    """Base for every blueprint model: unknown keys are an error, not a no-op."""

    model_config = ConfigDict(extra="forbid")


# ── Enums ─────────────────────────────────────────────────────────────────────


class StepAction(str, Enum):
    """Available step actions in a blueprint."""

    GOTO = "goto"
    FILL = "fill"
    CLICK = "click"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
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


# Values a step may interpolate. Anything else is a typo that would otherwise
# be typed literally into the site.
STEP_VARIABLES = frozenset({"username", "password"})
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")
_MAX_TIMEOUT_MS = 300_000


def _check_placeholders(value: Optional[str], field_name: str) -> None:
    if not value:
        return
    for name in _PLACEHOLDER.findall(value):
        if name not in STEP_VARIABLES:
            raise ValueError(
                f"{field_name} uses unknown variable '{{{{{name}}}}}'; available: "
                + ", ".join(sorted(f"{{{{{v}}}}}" for v in STEP_VARIABLES))
            )


# ── Step Models ───────────────────────────────────────────────────────────────


# Fields each action accepts ("action" is always allowed) and the ones it needs.
_STEP_FIELDS: Dict[StepAction, frozenset[str]] = {
    StepAction.GOTO: frozenset({"url", "timeout"}),
    StepAction.FILL: frozenset({"selector", "value", "timeout"}),
    StepAction.CLICK: frozenset({"selector", "timeout", "wait_for_navigation"}),
    StepAction.WAIT: frozenset({"selector", "timeout"}),
    StepAction.SCREENSHOT: frozenset({"screenshot_name"}),
    StepAction.CONDITIONAL: frozenset({"condition_selector", "then_steps", "else_steps", "timeout"}),
    StepAction.SCROLL: frozenset({"selector", "direction", "pixels"}),
    StepAction.SELECT: frozenset({"selector", "value", "timeout"}),
    StepAction.IFRAME: frozenset({"iframe_selector", "selector", "steps", "timeout"}),
    StepAction.WAIT_FOR_NAVIGATION: frozenset({"timeout"}),
    StepAction.EXECUTE_JS: frozenset({"script"}),
}
_STEP_REQUIRED: Dict[StepAction, frozenset[str]] = {
    StepAction.GOTO: frozenset({"url"}),
    StepAction.FILL: frozenset({"selector", "value"}),
    StepAction.CLICK: frozenset({"selector"}),
    StepAction.SELECT: frozenset({"selector", "value"}),
    StepAction.CONDITIONAL: frozenset({"condition_selector"}),
    StepAction.IFRAME: frozenset({"steps"}),
    StepAction.EXECUTE_JS: frozenset({"script"}),
}


class BlueprintStep(_StrictModel):
    """A single step in an auth or cleanup flow."""

    action: StepAction = Field(..., description="The action to perform.")
    url: Optional[str] = Field(None, description="URL for goto actions (http/https only).")
    selector: Optional[str] = Field(None, description="CSS selector for the target element.")
    value: Optional[str] = Field(
        None,
        description="Value to fill/select. Supports {{username}} and {{password}}.",
    )
    timeout: Optional[int] = Field(
        None,
        ge=1,
        le=_MAX_TIMEOUT_MS,
        description="Timeout in milliseconds for this step. A wait step with only a timeout pauses for that long.",
    )
    wait_for_navigation: Optional[bool] = Field(
        False,
        description="Wait for navigation to complete after this step.",
    )
    screenshot_name: Optional[str] = Field(
        None,
        description="Name for screenshot (taken only in debug mode, into a private per-run folder).",
    )
    script: Optional[str] = Field(
        None,
        description="JavaScript to execute (execute_js; trusted connectors only, auth and MFA phases only).",
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
        description="CSS selector for the iframe whose document `steps` run in.",
    )
    steps: Optional[List[BlueprintStep]] = Field(
        None,
        description="Steps to run inside the iframe (iframe action).",
    )
    direction: Optional[str] = Field(
        None,
        description="Scroll direction: 'down' or 'up'.",
    )
    pixels: Optional[int] = Field(
        None,
        ge=1,
        le=100_000,
        description="Number of pixels to scroll.",
    )

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in ("down", "up"):
            raise ValueError("direction must be 'down' or 'up'")
        return v

    @model_validator(mode="after")
    def validate_action_fields(self) -> BlueprintStep:
        present = {
            name
            for name in self.model_fields_set
            if name != "action"
            and getattr(self, name) is not None
            and not (name == "wait_for_navigation" and getattr(self, name) is False)
        }
        allowed = _STEP_FIELDS[self.action]
        unexpected = present - allowed
        if unexpected:
            raise ValueError(
                f"'{self.action.value}' steps do not take {', '.join(sorted(unexpected))} "
                f"(allowed: {', '.join(sorted(allowed)) or 'nothing'})"
            )
        missing = {name for name in _STEP_REQUIRED.get(self.action, ()) if not getattr(self, name)}
        if self.action == StepAction.FILL and self.value == "":
            missing.discard("value")  # filling an empty string clears a field
        if missing:
            raise ValueError(f"'{self.action.value}' steps require {', '.join(sorted(missing))}")

        if self.action == StepAction.WAIT and not (self.selector or self.timeout):
            raise ValueError("'wait' steps require a selector, or a timeout to pause for")
        if self.action == StepAction.IFRAME and not (self.iframe_selector or self.selector):
            raise ValueError("'iframe' steps require iframe_selector (or selector)")
        if self.action == StepAction.CONDITIONAL and not (self.then_steps or self.else_steps):
            raise ValueError("'conditional' steps require then_steps and/or else_steps")

        _check_placeholders(self.url, "url")
        _check_placeholders(self.value, "value")
        if self.action == StepAction.GOTO and self.url and not _PLACEHOLDER.search(self.url):
            scheme = split_url(self.url)[0]
            if scheme not in ALLOWED_SCHEMES:
                raise ValueError(f"goto only accepts http(s) URLs, not {scheme or 'relative'} URLs")
        return self


# ── Outcome checks ────────────────────────────────────────────────────────────


class OutcomeCheck(_StrictModel):
    """A page state that proves an outcome (e.g. "signed in", "wrong password").

    ``selector`` must be visible; ``text`` is a case-insensitive regular
    expression searched in that element's text, or in the whole page's visible
    text when no selector is given. At least one of the two is required.
    """

    selector: Optional[str] = Field(None, description="CSS selector that must be visible.")
    text: Optional[str] = Field(None, description="Case-insensitive regular expression to find in the visible text.")
    timeout: Optional[int] = Field(
        None,
        ge=1,
        le=_MAX_TIMEOUT_MS,
        description="Milliseconds to wait for this indicator (success checks).",
    )

    @model_validator(mode="after")
    def validate_check(self) -> OutcomeCheck:
        if not (self.selector or self.text):
            raise ValueError("an outcome check needs a selector and/or a text pattern")
        if self.text:
            try:
                re.compile(self.text)
            except re.error as exc:
                raise ValueError(f"text is not a valid regular expression: {exc}") from exc
        return self

    @property
    def pattern(self) -> Optional[re.Pattern[str]]:
        return re.compile(self.text, re.IGNORECASE) if self.text else None


def _validate_targets(targets: Optional[List[str]]) -> Optional[List[str]]:
    if targets is None:
        return None
    for target in targets:
        parse_target(target)
    return targets


# ── MFA Models ────────────────────────────────────────────────────────────────


class MFADetection(_StrictModel):
    """How to detect that MFA is required."""

    selector: str = Field(..., min_length=1, description="CSS selector that indicates MFA is needed.")
    timeout: int = Field(
        3000,
        ge=1,
        le=_MAX_TIMEOUT_MS,
        description="Milliseconds to wait for MFA detection after login.",
    )


_CODE_MFA_TYPES = frozenset({MFAType.OTP_INPUT, MFAType.EMAIL_CODE, MFAType.SECURITY_QUESTION})


class MFAConfig(_StrictModel):
    """MFA configuration for a blueprint."""

    detection: MFADetection = Field(..., description="How to detect MFA prompts.")
    type: MFAType = Field(..., description="Type of MFA expected.")
    handler: MFAHandler = Field(
        MFAHandler.USER_PROMPT,
        description="How MFA should be handled.",
    )
    input_selector: Optional[str] = Field(
        None,
        description="CSS selector for the MFA input field (required for code-based MFA).",
    )
    submit_selector: Optional[str] = Field(
        None,
        description="CSS selector for the MFA submit button. Without one, Enter is pressed in the input.",
    )
    question_selector: Optional[str] = Field(
        None,
        description="CSS selector for security question text.",
    )
    submit_targets: Optional[List[str]] = Field(
        None,
        description=(
            "URLs the MFA form may submit to: '/path' on the blueprint's domains or an absolute http(s) URL; "
            "'*' matches anything. Form submissions anywhere else are refused during MFA."
        ),
    )
    success: Optional[OutcomeCheck] = Field(None, description="Shown once the code is accepted.")
    failure: Optional[OutcomeCheck] = Field(None, description="Shown when the code is rejected.")
    poll_interval: Optional[int] = Field(
        2000,
        ge=100,
        le=60_000,
        description="Polling interval in ms for push MFA.",
    )
    poll_timeout: Optional[int] = Field(
        60000,
        ge=1000,
        le=3_600_000,
        description="Max wait time in ms for push MFA (capped by MFA_TIMEOUT_SECONDS).",
    )

    @field_validator("submit_targets")
    @classmethod
    def validate_submit_targets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_targets(v)

    @model_validator(mode="after")
    def validate_type_fields(self) -> MFAConfig:
        if self.type in _CODE_MFA_TYPES and not self.input_selector:
            raise ValueError(f"'{self.type.value}' MFA requires input_selector (where the code is typed)")
        return self


# ── Extraction Models ────────────────────────────────────────────────────────


class ExtractionField(_StrictModel):
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
        description="If true, the value is never logged and is listed in the result's metadata.sensitive_fields.",
    )
    required: bool = Field(
        False,
        description="If true, a missing value fails the extraction instead of coming back as null.",
    )
    attribute: Optional[str] = Field(
        None,
        description="HTML attribute to extract instead of text content (e.g., 'href', 'value').",
    )
    default: Optional[Any] = Field(
        None,
        description="Value to use when the element is absent.",
    )
    timeout: Optional[int] = Field(
        None,
        ge=1,
        le=_MAX_TIMEOUT_MS,
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

    @field_validator("type")
    @classmethod
    def validate_scalar_type(cls, v: FieldType) -> FieldType:
        if v in (FieldType.LIST, FieldType.TABLE):
            raise ValueError("list/table fields need a 'fields' map of columns")
        return v


class PaginationConfig(_StrictModel):
    """Configuration for paginated data extraction."""

    next_selector: str = Field(..., min_length=1, description="CSS selector for the 'next page' button.")
    max_pages: int = Field(5, ge=1, le=100, description="Maximum number of pages to traverse.")
    wait_after_click: int = Field(2000, ge=0, le=60_000, description="ms to wait after clicking next.")


class ListExtractionField(_StrictModel):
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
        min_length=1,
        description="Fields to extract from each row.",
    )
    max_items: Optional[int] = Field(
        None,
        ge=1,
        description="Maximum number of items to extract.",
    )
    pagination: Optional[PaginationConfig] = Field(
        None,
        description="Pagination configuration for multi-page extraction.",
    )
    required: bool = Field(
        False,
        description="If true, an empty or missing list fails the extraction.",
    )

    @field_validator("type")
    @classmethod
    def validate_list_type(cls, v: FieldType) -> FieldType:
        if v not in (FieldType.LIST, FieldType.TABLE):
            raise ValueError("type must be 'list' or 'table'")
        return v


# ── Rate Limit & Health ──────────────────────────────────────────────────────


class ExtractionStrategy(str, Enum):
    """How data extraction should be performed."""

    SELECTOR = "selector"  # V2: hardcoded CSS selectors
    LLM_ADAPTIVE = "llm_adaptive"  # V3: LLM-based with selector caching


class RateLimitConfig(_StrictModel):
    """How hard Plaidify may hit the site, per account (site + username)."""

    max_requests_per_hour: int = Field(
        10,
        ge=1,
        le=100_000,
        description="Maximum connections per hour for one account on this site.",
    )
    min_interval_seconds: int = Field(
        30,
        ge=0,
        le=86_400,
        description="Minimum seconds between two connections for one account on this site.",
    )


class HealthCheckConfig(_StrictModel):
    """Health check for the target site."""

    url: str = Field(..., description="URL to check for site availability.")
    expected_status: int = Field(200, ge=100, le=599, description="Expected HTTP status code.")

    @field_validator("url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        if split_url(v)[0] not in ALLOWED_SCHEMES:
            raise ValueError("health_check.url must be an http(s) URL")
        return v


# ── Auth Config ──────────────────────────────────────────────────────────────


class AuthConfig(_StrictModel):
    """Authentication configuration."""

    type: AuthType = Field(AuthType.FORM, description="Authentication method.")
    steps: List[BlueprintStep] = Field(
        ...,
        min_length=1,
        description="Ordered steps to perform authentication.",
    )
    submit_targets: Optional[List[str]] = Field(
        None,
        description=(
            "URLs the login form may submit to: '/path' on the blueprint's domains or an absolute http(s) URL; "
            "'*' matches anything. Form submissions anywhere else are refused during login."
        ),
    )
    success: Optional[OutcomeCheck] = Field(
        None,
        description="Shown once signed in. Waited for after the auth steps (MFA prompts also end the wait).",
    )
    failure: Optional[OutcomeCheck] = Field(
        None,
        description="Shown when the site rejects the credentials; reported as invalid_credentials.",
    )

    @field_validator("submit_targets")
    @classmethod
    def validate_submit_targets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_targets(v)


# ── Top-Level Blueprint ──────────────────────────────────────────────────────


class BlueprintV2(_StrictModel):
    """
    Blueprint V2/V3 — the complete definition for connecting to a website.

    Defines authentication flow, MFA handling, data extraction, cleanup,
    rate limiting, and health checks.

    V2: All extraction fields must have CSS selectors.
    V3: Adds 'llm_adaptive' strategy — fields use descriptions instead of selectors,
        and the LLM figures out the correct selectors at runtime.
    """

    schema_version: str = Field(
        ...,
        description="Blueprint schema version: '2.0' or '3.0'.",
    )
    name: str = Field(..., min_length=1, description="Human-readable site name.")
    domain: str = Field(
        ...,
        description="Target website host[:port]. The browser may navigate to it and its subdomains only.",
    )
    description: Optional[str] = Field(None, description="What this connector reaches and extracts.")
    tags: List[str] = Field(
        default_factory=list,
        description="Tags for categorization (e.g., 'banking', 'us').",
    )
    allowed_domains: List[str] = Field(
        default_factory=list,
        description="Extra hosts (host[:port] or *.host) the browser may navigate to, e.g. a separate login domain.",
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
    logout_targets: Optional[List[str]] = Field(
        None,
        description=(
            "URLs cleanup may navigate or submit to ('/path' or absolute http(s) URL, '*' wildcards). "
            "Cleanup can reach nothing else."
        ),
    )
    rate_limit: Optional[RateLimitConfig] = Field(
        None,
        description="Rate limiting configuration.",
    )
    health_check: Optional[HealthCheckConfig] = Field(
        None,
        description="Health check configuration.",
    )
    credential_schema: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional UI-facing schema describing credential fields for the hosted "
            "Link flow (#54). Keys: `fields` (list of field descriptors) and "
            "`submit_label`. Each field has `id`, `label`, `type` (text|email|"
            "password|tel|number), optional `autocomplete`, `inputmode`, "
            "`placeholder`, `help_text`, `pattern`, `min_length`, `max_length`, "
            "`required`, `secret`, `reveal`. When omitted, the frontend derives "
            "a default from the organization's `auth_style`."
        ),
    )
    mfa_schema: Optional[Dict[str, Any]] = Field(
        None,
        description=(
            "Optional UI-facing schema describing MFA prompts keyed by MFA "
            "type (sms, totp, security_question, push, email_code). Each entry "
            "has a `title`, `help_text`, `fields` (same descriptor shape as "
            "credential_schema), and optional `submit_label`. When omitted, "
            "the frontend renders a single numeric `code` field."
        ),
    )

    @field_validator("schema_version")
    @classmethod
    def validate_schema_version(cls, v: str) -> str:
        if v not in ("2.0", "3.0"):
            raise ValueError(
                f"Unsupported schema version: {v}. Expected '2.0' or '3.0' ('1.0' files are converted on load)."
            )
        return v

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        parse_domain_rule(v)
        return v.strip()

    @field_validator("allowed_domains")
    @classmethod
    def validate_allowed_domains(cls, v: List[str]) -> List[str]:
        for entry in v:
            parse_domain_rule(entry)
        return v

    @field_validator("logout_targets")
    @classmethod
    def validate_logout_targets(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        return _validate_targets(v)

    @model_validator(mode="after")
    def validate_blueprint(self) -> BlueprintV2:
        rules = self.host_rules()

        declared = [
            *(self.auth.submit_targets or []),
            *((self.mfa.submit_targets or []) if self.mfa else []),
            *(self.logout_targets or []),
        ]
        for target in declared:
            pattern = parse_target(target)
            if pattern.host_rule is not None and not any(
                rule.allows(pattern.host_rule.host, pattern.host_rule.port) for rule in rules
            ):
                raise ValueError(f"target {target!r} is not on the blueprint's domain or allowed_domains")

        if self.extraction_strategy == ExtractionStrategy.SELECTOR:
            for name, field_def in self.extract.items():
                if not field_def.selector:
                    raise ValueError(f"extract.{name} needs a selector (only llm_adaptive blueprints may omit them)")
                if isinstance(field_def, ListExtractionField):
                    for col, col_def in field_def.fields.items():
                        if not col_def.selector:
                            raise ValueError(f"extract.{name}.fields.{col} needs a selector")

        if self.fallback_selectors:
            unknown = set(self.fallback_selectors) - set(self.extract)
            if unknown:
                raise ValueError(f"fallback_selectors name unknown fields: {', '.join(sorted(unknown))}")
        return self

    @property
    def is_llm_adaptive(self) -> bool:
        """Check if this blueprint uses LLM-adaptive extraction."""
        return self.extraction_strategy == ExtractionStrategy.LLM_ADAPTIVE

    def host_rules(self) -> List[HostRule]:
        """Hosts the browser may navigate to: the blueprint's domain plus allowed_domains."""
        return [parse_domain_rule(self.domain), *(parse_domain_rule(d) for d in self.allowed_domains)]

    def auth_targets(self) -> List[TargetPattern]:
        return [parse_target(t) for t in (self.auth.submit_targets or [])]

    def mfa_targets(self) -> List[TargetPattern]:
        return [parse_target(t) for t in ((self.mfa.submit_targets or []) if self.mfa else [])]

    def cleanup_targets(self) -> List[TargetPattern]:
        return [parse_target(t) for t in (self.logout_targets or [])]

    def sensitive_field_names(self) -> List[str]:
        """Names of values marked sensitive, as ``field`` or ``list_field[].column``."""
        names: List[str] = []
        for name, field_def in self.extract.items():
            if isinstance(field_def, ListExtractionField):
                names.extend(f"{name}[].{col}" for col, col_def in field_def.fields.items() if col_def.sensitive)
            elif field_def.sensitive:
                names.append(name)
        return names


# ── Legacy V1 Conversion ─────────────────────────────────────────────────────

_V1_KEYS = frozenset({"schema_version", "name", "login_url", "fields", "post_login"})


def convert_v1_to_v2(v1_data: dict) -> BlueprintV2:
    """
    Convert a V1 blueprint (current format) to a V2 BlueprintV2 model.

    V1 format:
        {
            "schema_version": "1.0",
            "name": "...",
            "login_url": "...",
            "fields": {"username": "#user", "password": "#pass", "submit": "#login-btn"},
            "post_login": [{"wait": "..."}, {"extract": {...}}]
        }

    V1 files cannot declare where the login form posts, so the converted
    blueprint allows form submissions anywhere on the login URL's own host.

    Args:
        v1_data: Raw V1 blueprint dictionary.

    Returns:
        BlueprintV2 model instance.
    """
    unknown = set(v1_data) - _V1_KEYS
    if unknown:
        raise ValueError(f"Unknown keys in a 1.0 blueprint: {', '.join(sorted(unknown))}")

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

    # Derive the domain (host[:port], never credentials) from login_url
    _scheme, host, port, _path, _userinfo = split_url(login_url)
    domain = host
    default_port = {"http": 80, "https": 443}.get(_scheme)
    if host and port and port != default_port:
        domain = f"{host}:{port}"

    return BlueprintV2(
        schema_version="2.0",
        name=name,
        domain=domain,
        tags=[],
        auth=AuthConfig(
            type=AuthType.FORM,
            steps=[BlueprintStep(**s) for s in auth_steps],
            submit_targets=["/*"],
        ),
        extract={k: ExtractionField(**v) for k, v in extract_fields.items()},
    )


def load_blueprint_from_dict(data: Any) -> BlueprintV2:
    """Load and validate a blueprint from a dictionary.

    ``schema_version`` is required: '2.0' and '3.0' documents are validated as
    they are, and '1.0' documents (``login_url`` style) are converted.

    Raises:
        ValueError / pydantic.ValidationError: if the blueprint is invalid.
    """
    if not isinstance(data, dict):
        raise ValueError("A blueprint must be a JSON object.")

    version = data.get("schema_version")
    if version is None:
        raise ValueError("schema_version is required: '2.0' or '3.0' (or '1.0' for legacy login_url blueprints).")
    if str(version) == "1.0":
        return convert_v1_to_v2(data)
    return BlueprintV2.model_validate(data)


def load_blueprint(path: Path) -> BlueprintV2:
    """
    Load and parse a blueprint from a JSON file.

    Args:
        path: Path to the blueprint JSON file.

    Returns:
        Validated BlueprintV2 model.

    Raises:
        json.JSONDecodeError: If the file contains invalid JSON.
        ValueError / pydantic.ValidationError: If the blueprint fails schema validation.
    """
    with open(path) as f:
        data = json.load(f)
    return load_blueprint_from_dict(data)


# ── Discovery, execution and trust ───────────────────────────────────────────

# Tags that hide a connector from every public discovery surface.
HIDDEN_CONNECTOR_TAGS = frozenset({"internal", "fixture"})
# Tags that mark a connector as a bundled sandbox/demo — discoverable only
# when demo mode is enabled.
SANDBOX_CONNECTOR_TAGS = frozenset({"sandbox", "demo"})


def _tag_set(tags: Optional[Iterable[str]]) -> set[str]:
    return {str(t).lower() for t in (tags or [])}


def blueprint_is_discoverable(tags: Optional[List[str]], *, demo_mode: bool = False) -> bool:
    """Return whether a connector with these tags should appear on public
    discovery surfaces (``/blueprints``, ``/blueprints/{site}``, the picker
    catalog).

    Rules:
    - Connectors tagged ``internal`` or ``fixture`` are never discoverable.
    - Connectors tagged ``sandbox`` or ``demo`` are discoverable only when
      ``demo_mode`` is enabled.
    - All other connectors are discoverable.
    """
    tag_set = _tag_set(tags)
    if tag_set & HIDDEN_CONNECTOR_TAGS:
        return False
    if tag_set & SANDBOX_CONNECTOR_TAGS and not demo_mode:
        return False
    return True


def blueprint_is_executable(
    tags: Optional[Iterable[str]],
    *,
    demo_mode: bool = False,
    allow_internal: bool = False,
) -> bool:
    """Whether a connector with these tags may run at all.

    Internal, fixture, sandbox and demo connectors drive the server's browser
    at local test portals; they run only in demo mode or when tests explicitly
    allow it. Everything else runs.
    """
    if _tag_set(tags) & (HIDDEN_CONNECTOR_TAGS | SANDBOX_CONNECTOR_TAGS):
        return demo_mode or allow_internal
    return True


class TrustTier(str, Enum):
    """How much a blueprint is trusted.

    ``bundled`` blueprints ship in this repository's connectors/ directory;
    ``operator`` ones are named in ENGINE_TRUSTED_CONNECTORS. Everything else —
    generated, downloaded from the registry, or dropped in by a user — is
    ``untrusted``: no JavaScript, and never a private network.
    """

    BUNDLED = "bundled"
    OPERATOR = "operator"
    UNTRUSTED = "untrusted"

    @property
    def is_trusted(self) -> bool:
        return self is not TrustTier.UNTRUSTED

    @property
    def allows_javascript(self) -> bool:
        return self.is_trusted


# The connectors shipped with Plaidify. A file only earns the bundled tier if it
# sits in this repository's connectors/ directory under one of these names, so
# a generated or user-supplied blueprint can never claim it.
BUNDLED_CONNECTORS_DIR = Path(__file__).resolve().parents[2] / "connectors"
BUNDLED_CONNECTOR_SITES = frozenset({"demo_bank", "demo_saas", "demo_utility", "hydro_one", "internal_bank"})
GENERATED_TAG = "auto_generated"


def resolve_trust_tier(
    path: Path,
    blueprint: BlueprintV2,
    *,
    operator_trusted: Iterable[str] = (),
) -> TrustTier:
    """Decide how far to trust the blueprint loaded from ``path``."""
    if GENERATED_TAG in _tag_set(blueprint.tags):
        return TrustTier.UNTRUSTED
    resolved = Path(path).resolve()
    if resolved.parent == BUNDLED_CONNECTORS_DIR and resolved.stem in BUNDLED_CONNECTOR_SITES:
        return TrustTier.BUNDLED
    if resolved.stem in {site.strip() for site in operator_trusted if site.strip()}:
        return TrustTier.OPERATOR
    return TrustTier.UNTRUSTED
