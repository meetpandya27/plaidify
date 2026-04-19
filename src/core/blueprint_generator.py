"""
Blueprint Auto-Generator — LLM-powered blueprint creation for arbitrary websites.

Takes a URL, navigates to it with a headless browser, captures the DOM and
a screenshot, then uses an LLM (text + vision) to identify login form elements
and generate a V3 blueprint with llm_adaptive extraction.

This is the missing piece that turns Plaidify from "blueprint-first" into
"search any website and Plaidify it."

Usage:
    generator = BlueprintGenerator(llm_provider)
    result = await generator.generate("https://mybank.com/login")
    # result.blueprint — BlueprintV2 instance
    # result.blueprint_json — dict ready to save as JSON
    # result.confidence — 0.0–1.0 confidence score
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from src.core.blueprint import (
    BlueprintV2,
)
from src.core.dom_simplifier import DOMSimplifier
from src.core.llm_provider import BaseLLMProvider, LLMProviderError
from src.logging_config import get_logger

logger = get_logger("blueprint_generator")

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_SCREENSHOT_WIDTH = 1280
MAX_SCREENSHOT_HEIGHT = 1024

# System prompt for login-form discovery
LOGIN_DISCOVERY_SYSTEM_PROMPT = """You are a web automation expert that analyzes login pages to create automation blueprints.

Given simplified HTML of a login page, identify:
1. The username/email input field (CSS selector)
2. The password input field (CSS selector)
3. The login/submit button (CSS selector)
4. Whether MFA might be present after login (look for hints like "2FA", "verification", "OTP")
5. Any additional pre-login steps needed (e.g., cookie consent dismiss, "personal banking" tab click)

Return ONLY valid JSON matching this exact schema:
{
  "username_selector": "<CSS selector for username/email input>",
  "password_selector": "<CSS selector for password input>",
  "submit_selector": "<CSS selector for login button>",
  "pre_login_steps": [
    {"action": "click", "selector": "<CSS selector>", "description": "<why>"}
  ],
  "mfa_likely": true/false,
  "mfa_hints": "<description of MFA indicators if any>",
  "page_title": "<detected page title or site name>",
  "confidence": 0.0-1.0
}

Rules:
- Use the most specific, stable CSS selectors possible (prefer id > name > aria-label > data attributes > type+class combos).
- For selectors, prefer #id, input[name="..."], button[type="submit"], [aria-label="..."].
- Do NOT use positional selectors like :nth-child unless no better option exists.
- If you cannot identify a field, set its selector to null.
- pre_login_steps should be empty [] unless there are mandatory clicks before the form is usable.
- Set confidence based on how certain you are about the selectors (1.0 = very certain, 0.3 = guessing)."""

# System prompt for vision-based login discovery (fallback)
VISION_LOGIN_SYSTEM_PROMPT = """You are a web automation expert that analyzes screenshots of login pages.

Look at this screenshot of a login page and identify:
1. The username/email input field
2. The password input field
3. The login/submit button
4. Any cookie banners or overlays that need dismissing first

Based on visible labels, placeholders, and layout, suggest CSS selectors.

Return ONLY valid JSON:
{
  "username_selector": "<best guess CSS selector>",
  "password_selector": "<best guess CSS selector>",
  "submit_selector": "<best guess CSS selector>",
  "pre_login_steps": [],
  "mfa_likely": true/false,
  "mfa_hints": "",
  "page_title": "<site name from screenshot>",
  "confidence": 0.0-1.0
}"""

# System prompt for extraction field discovery
FIELD_DISCOVERY_SYSTEM_PROMPT = """You are a data extraction expert. Given a description of a website type, suggest common data fields that could be extracted after login.

Return ONLY valid JSON:
{
  "fields": [
    {
      "name": "<field_name_snake_case>",
      "type": "text|currency|date|number|email|phone",
      "description": "<what this field contains>",
      "example": "<example value>",
      "sensitive": true/false
    }
  ],
  "page_context": "<description of what the dashboard/main page likely shows>"
}

Rules:
- Suggest 5-15 fields that are commonly available on this type of site.
- Use snake_case for field names.
- Mark account numbers, SSNs, passwords as sensitive.
- Include common fields like account_holder_name, account_number, balance, etc."""


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class LoginFormDiscovery:
    """Result of analyzing a login page."""

    username_selector: Optional[str]
    password_selector: Optional[str]
    submit_selector: Optional[str]
    pre_login_steps: List[Dict[str, str]]
    mfa_likely: bool
    mfa_hints: str
    page_title: str
    confidence: float


@dataclass
class GeneratedBlueprint:
    """Result of blueprint generation."""

    blueprint: BlueprintV2
    blueprint_json: Dict[str, Any]
    confidence: float
    login_url: str
    domain: str
    site_key: str
    warnings: List[str] = field(default_factory=list)


# ── Blueprint Generator ──────────────────────────────────────────────────────


class BlueprintGenerator:
    """Generates V3 blueprints for arbitrary websites using LLM analysis."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        vision_provider: Optional[BaseLLMProvider] = None,
    ):
        self.provider = provider
        self.vision_provider = vision_provider or provider
        self.dom_simplifier = DOMSimplifier()

    async def generate(
        self,
        url: str,
        page: Any,  # playwright.async_api.Page
        *,
        site_type: Optional[str] = None,
        site_name: Optional[str] = None,
        extra_fields: Optional[List[Dict[str, str]]] = None,
    ) -> GeneratedBlueprint:
        """Generate a V3 blueprint for a website.

        Args:
            url: The login page URL.
            page: An already-navigated Playwright Page object.
            site_type: Optional hint (e.g., "banking", "utility", "insurance").
            site_name: Optional human-readable name for the site.
            extra_fields: Optional additional fields to include in extraction.

        Returns:
            GeneratedBlueprint with the generated blueprint and metadata.
        """
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.hostname or "unknown"
        site_key = _domain_to_site_key(domain)

        logger.info(
            "Generating blueprint for %s (domain=%s, site_key=%s)",
            url,
            domain,
            site_key,
        )

        warnings: List[str] = []

        # Step 1: Analyze login form via DOM
        login_discovery = await self._discover_login_form_dom(page)

        # Step 2: If DOM analysis has low confidence, try vision fallback
        if login_discovery.confidence < 0.5:
            logger.info(
                "DOM analysis low confidence (%.2f), trying vision fallback",
                login_discovery.confidence,
            )
            vision_discovery = await self._discover_login_form_vision(page)
            if vision_discovery.confidence > login_discovery.confidence:
                login_discovery = vision_discovery
                warnings.append("Used vision-based analysis (DOM analysis was low confidence).")

        # Validate we found the critical selectors
        if not login_discovery.username_selector or not login_discovery.password_selector:
            warnings.append(
                "Could not identify username and/or password fields. Blueprint auth steps may need manual correction."
            )

        if not login_discovery.submit_selector:
            warnings.append("Could not identify login submit button. Blueprint uses form submit fallback.")

        # Step 3: Discover extraction fields based on site type
        extraction_fields = await self._discover_fields(
            site_type=site_type,
            domain=domain,
            page_title=login_discovery.page_title or site_name or domain,
        )

        if extra_fields:
            for ef in extra_fields:
                extraction_fields["fields"].append(ef)

        # Step 4: Build the blueprint
        blueprint_json = self._build_blueprint_json(
            url=url,
            domain=domain,
            site_key=site_key,
            site_name=site_name or login_discovery.page_title or domain,
            login_discovery=login_discovery,
            extraction_fields=extraction_fields,
            site_type=site_type,
        )

        # Step 5: Validate via Pydantic
        blueprint = BlueprintV2.model_validate(blueprint_json)

        return GeneratedBlueprint(
            blueprint=blueprint,
            blueprint_json=blueprint_json,
            confidence=login_discovery.confidence,
            login_url=url,
            domain=domain,
            site_key=site_key,
            warnings=warnings,
        )

    # ── DOM-Based Login Discovery ─────────────────────────────────────────────

    async def _discover_login_form_dom(self, page: Any) -> LoginFormDiscovery:
        """Analyze the page DOM to find login form elements."""
        simplified = await self.dom_simplifier.simplify(page)

        prompt = (
            "## Login Page HTML\n"
            "Analyze this simplified HTML and identify the login form elements.\n\n"
            "```html\n"
            f"{simplified.html}\n"
            "```\n"
        )

        try:
            response = await self.provider.extract(
                prompt,
                system_prompt=LOGIN_DISCOVERY_SYSTEM_PROMPT,
                max_tokens=2048,
                json_mode=True,
            )
            result = response.parse_json()
        except (LLMProviderError, json.JSONDecodeError, ValueError) as e:
            logger.warning("DOM-based login discovery failed: %s", e)
            return LoginFormDiscovery(
                username_selector=None,
                password_selector=None,
                submit_selector=None,
                pre_login_steps=[],
                mfa_likely=False,
                mfa_hints="",
                page_title="",
                confidence=0.0,
            )

        return LoginFormDiscovery(
            username_selector=result.get("username_selector"),
            password_selector=result.get("password_selector"),
            submit_selector=result.get("submit_selector"),
            pre_login_steps=result.get("pre_login_steps", []),
            mfa_likely=bool(result.get("mfa_likely", False)),
            mfa_hints=str(result.get("mfa_hints", "")),
            page_title=str(result.get("page_title", "")),
            confidence=float(result.get("confidence", 0.0)),
        )

    # ── Vision-Based Login Discovery ──────────────────────────────────────────

    async def _discover_login_form_vision(self, page: Any) -> LoginFormDiscovery:
        """Analyze a screenshot to find login form elements (fallback)."""
        try:
            screenshot_bytes = await page.screenshot(
                type="png",
                full_page=False,
            )
        except Exception as e:
            logger.warning("Screenshot capture failed: %s", e)
            return LoginFormDiscovery(
                username_selector=None,
                password_selector=None,
                submit_selector=None,
                pre_login_steps=[],
                mfa_likely=False,
                mfa_hints="",
                page_title="",
                confidence=0.0,
            )

        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        messages = [
            {"role": "system", "content": VISION_LOGIN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Analyze this login page screenshot and identify the form elements.",
                    },
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{screenshot_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

        try:
            response = await self.vision_provider._call(messages)
            result = response.parse_json()
        except (LLMProviderError, json.JSONDecodeError, ValueError) as e:
            logger.warning("Vision-based login discovery failed: %s", e)
            return LoginFormDiscovery(
                username_selector=None,
                password_selector=None,
                submit_selector=None,
                pre_login_steps=[],
                mfa_likely=False,
                mfa_hints="",
                page_title="",
                confidence=0.0,
            )

        return LoginFormDiscovery(
            username_selector=result.get("username_selector"),
            password_selector=result.get("password_selector"),
            submit_selector=result.get("submit_selector"),
            pre_login_steps=result.get("pre_login_steps", []),
            mfa_likely=bool(result.get("mfa_likely", False)),
            mfa_hints=str(result.get("mfa_hints", "")),
            page_title=str(result.get("page_title", "")),
            confidence=float(result.get("confidence", 0.0)),
        )

    # ── Field Discovery ───────────────────────────────────────────────────────

    async def _discover_fields(
        self,
        *,
        site_type: Optional[str],
        domain: str,
        page_title: str,
    ) -> Dict[str, Any]:
        """Use LLM to suggest extraction fields for this type of site."""
        type_hint = site_type or "general website"
        prompt = (
            f"## Site Information\n"
            f"- Domain: {domain}\n"
            f"- Page Title: {page_title}\n"
            f"- Site Type: {type_hint}\n\n"
            f"Suggest data fields that could be extracted after logging in to this type of site."
        )

        try:
            response = await self.provider.extract(
                prompt,
                system_prompt=FIELD_DISCOVERY_SYSTEM_PROMPT,
                max_tokens=2048,
                json_mode=True,
            )
            return response.parse_json()
        except (LLMProviderError, json.JSONDecodeError, ValueError) as e:
            logger.warning("Field discovery failed: %s", e)
            # Return minimal default fields
            return {
                "fields": [
                    {
                        "name": "account_name",
                        "type": "text",
                        "description": "Account holder name",
                        "sensitive": False,
                    },
                    {
                        "name": "account_number",
                        "type": "text",
                        "description": "Account number or identifier",
                        "sensitive": True,
                    },
                    {
                        "name": "balance",
                        "type": "currency",
                        "description": "Current account balance",
                        "sensitive": False,
                    },
                ],
                "page_context": f"Dashboard for {domain}",
            }

    # ── Blueprint Builder ─────────────────────────────────────────────────────

    def _build_blueprint_json(
        self,
        *,
        url: str,
        domain: str,
        site_key: str,
        site_name: str,
        login_discovery: LoginFormDiscovery,
        extraction_fields: Dict[str, Any],
        site_type: Optional[str],
    ) -> Dict[str, Any]:
        """Assemble the final blueprint JSON from discovered components."""

        # Build auth steps
        auth_steps: List[Dict[str, Any]] = []

        # Step 1: Navigate to login URL
        auth_steps.append(
            {
                "action": "goto",
                "url": url,
            }
        )

        # Pre-login steps (e.g., dismiss cookie banner)
        for step in login_discovery.pre_login_steps:
            if step.get("selector"):
                auth_steps.append(
                    {
                        "action": step.get("action", "click"),
                        "selector": step["selector"],
                    }
                )

        # Step 2: Fill username
        if login_discovery.username_selector:
            auth_steps.append(
                {
                    "action": "fill",
                    "selector": login_discovery.username_selector,
                    "value": "{{username}}",
                }
            )

        # Step 3: Fill password
        if login_discovery.password_selector:
            auth_steps.append(
                {
                    "action": "fill",
                    "selector": login_discovery.password_selector,
                    "value": "{{password}}",
                }
            )

        # Step 4: Click submit
        if login_discovery.submit_selector:
            auth_steps.append(
                {
                    "action": "click",
                    "selector": login_discovery.submit_selector,
                    "wait_for_navigation": True,
                }
            )

        # Step 5: Wait for post-login page load
        auth_steps.append(
            {
                "action": "wait",
                "timeout": 10000,
            }
        )

        # Build extraction fields
        extract: Dict[str, Any] = {}
        for f in extraction_fields.get("fields", []):
            fname = f.get("name", "unnamed")
            extract[fname] = {
                "type": f.get("type", "text"),
                "description": f.get("description", ""),
            }
            if f.get("example"):
                extract[fname]["example"] = f["example"]
            if f.get("sensitive"):
                extract[fname]["sensitive"] = True

        # Build tags
        tags = ["auto_generated"]
        if site_type:
            tags.append(site_type)

        # Assemble blueprint
        blueprint: Dict[str, Any] = {
            "schema_version": "3.0",
            "name": site_name,
            "domain": domain,
            "tags": tags,
            "extraction_strategy": "llm_adaptive",
            "page_context": extraction_fields.get(
                "page_context",
                f"Dashboard for {site_name}",
            ),
            "auth": {
                "type": "form",
                "steps": auth_steps,
            },
            "extract": extract,
            "rate_limit": {
                "max_requests_per_hour": 10,
                "min_interval_seconds": 30,
            },
        }

        # Add MFA config if likely
        if login_discovery.mfa_likely:
            blueprint["mfa"] = {
                "detection": {
                    "selector": "input[type='tel'], input[name*='otp'], input[name*='code'], input[name*='mfa'], [class*='mfa'], [class*='otp'], [class*='verification']",
                    "timeout": 5000,
                },
                "type": "otp_input",
                "handler": "user_prompt",
                "input_selector": "input[type='tel'], input[name*='otp'], input[name*='code']",
                "submit_selector": "button[type='submit'], input[type='submit']",
            }

        return blueprint


# ── Helpers ───────────────────────────────────────────────────────────────────


def _domain_to_site_key(domain: str) -> str:
    """Convert a domain like 'my-bank.com' to a safe site key 'my_bank_com'."""
    # Remove port
    domain = domain.split(":")[0]
    # Replace dots and hyphens with underscores
    key = re.sub(r"[.\-]+", "_", domain)
    # Remove any non-alphanumeric/underscore chars
    key = re.sub(r"[^a-zA-Z0-9_]", "", key)
    # Remove leading/trailing underscores
    key = key.strip("_").lower()
    return key or "unknown_site"
