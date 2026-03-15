"""
Multimodal Extractor — screenshot-based extraction using vision LLMs.

Fallback when DOM-based LLM extraction fails or returns low-confidence results.
Captures a PNG screenshot via Playwright and sends it to a multimodal model
(GPT-4o vision, Claude vision) alongside field definitions.

Usage:
    extractor = MultimodalExtractor(provider)
    result = await extractor.extract_from_screenshot(page, fields)
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from src.core.extraction_prompt import (
    ExtractionResult,
    FieldDefinition,
    ListFieldDefinition,
    build_data_schema,
    parse_extraction_json,
)
from src.core.llm_provider import (
    BaseLLMProvider,
    LLMProviderError,
    LLMResponse,
    TokenUsage,
)
from src.logging_config import get_logger

logger = get_logger("multimodal_extractor")

# ── Constants ─────────────────────────────────────────────────────────────────

# Default max screenshot dimensions (width, height) in pixels
MAX_SCREENSHOT_WIDTH = 1280
MAX_SCREENSHOT_HEIGHT = 1024

# Default confidence threshold — below this, extraction is considered failed
DEFAULT_CONFIDENCE_THRESHOLD = 0.3

# Vision-specific system prompt
VISION_SYSTEM_PROMPT = """You are a data extraction assistant that extracts structured data from screenshots of web pages.

Rules:
1. Extract ONLY the fields requested — do not invent data.
2. If a field cannot be found in the screenshot, set its value to null.
3. Return ONLY valid JSON matching the exact schema provided — no explanations or commentary.
4. Apply the type coercion described for each field (e.g., currency → number, date → ISO format).
5. Never include sensitive data in explanations — only in the designated value fields.
6. For list/table fields, extract all visible rows."""


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MultimodalExtractionResult:
    """Result from a multimodal (screenshot-based) extraction."""

    data: Dict[str, Any]
    confidence: float
    token_usage: TokenUsage
    latency_ms: float
    screenshot_size_bytes: int


# ── Multimodal Extractor ─────────────────────────────────────────────────────


class MultimodalExtractor:
    """Extracts structured data from page screenshots using vision LLMs."""

    def __init__(
        self,
        provider: BaseLLMProvider,
        *,
        max_width: int = MAX_SCREENSHOT_WIDTH,
        max_height: int = MAX_SCREENSHOT_HEIGHT,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ):
        self.provider = provider
        self.max_width = max_width
        self.max_height = max_height
        self.confidence_threshold = confidence_threshold

    async def extract_from_screenshot(
        self,
        page: Any,  # playwright.async_api.Page
        fields: List[FieldDefinition | ListFieldDefinition],
        *,
        page_context: Optional[str] = None,
        full_page: bool = False,
    ) -> MultimodalExtractionResult:
        """Capture a screenshot and extract data using a vision model.

        Args:
            page: Playwright Page object.
            fields: Fields to extract.
            page_context: Optional description of the page content.
            full_page: Whether to capture the full scrollable page.

        Returns:
            MultimodalExtractionResult with extracted data.

        Raises:
            LLMProviderError: If the vision model call fails.
        """
        # Capture screenshot
        screenshot_bytes = await self._capture_screenshot(page, full_page=full_page)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        logger.info(
            "Captured screenshot: size=%d bytes, full_page=%s",
            len(screenshot_bytes),
            full_page,
        )

        # Build the vision prompt
        text_prompt = self._build_vision_prompt(fields, page_context=page_context)

        # Call the vision model
        response = await self._call_vision_model(text_prompt, screenshot_b64)

        # Parse the response
        result = self._parse_vision_response(response)

        return MultimodalExtractionResult(
            data=result.data,
            confidence=result.confidence,
            token_usage=response.usage,
            latency_ms=response.latency_ms,
            screenshot_size_bytes=len(screenshot_bytes),
        )

    async def _capture_screenshot(
        self,
        page: Any,
        *,
        full_page: bool = False,
    ) -> bytes:
        """Capture a PNG screenshot of the page.

        Args:
            page: Playwright Page object.
            full_page: Whether to capture the full scrollable page.

        Returns:
            PNG image bytes.
        """
        # Set viewport to consistent size for reproducibility
        viewport = page.viewport_size
        if viewport and (viewport["width"] > self.max_width or viewport["height"] > self.max_height):
            await page.set_viewport_size(
                {"width": self.max_width, "height": self.max_height}
            )

        return await page.screenshot(
            type="png",
            full_page=full_page,
        )

    def _build_vision_prompt(
        self,
        fields: List[FieldDefinition | ListFieldDefinition],
        *,
        page_context: Optional[str] = None,
    ) -> str:
        """Build the text portion of the vision extraction prompt."""
        field_specs = [f.to_prompt_dict() for f in fields]
        output_schema = self._build_output_schema(fields)

        parts = [
            "## Task\nExtract the following fields from the screenshot of a web page.\n",
        ]

        if page_context:
            parts.append(f"## Page Context\n{page_context}\n")

        parts.append("## Fields to Extract")
        parts.append("```json")
        parts.append(json.dumps(field_specs, indent=2))
        parts.append("```\n")

        parts.append("## Expected Output Schema")
        parts.append("Return JSON matching this exact structure:")
        parts.append("```json")
        parts.append(json.dumps(output_schema, indent=2))
        parts.append("```")

        return "\n".join(parts)

    async def _call_vision_model(
        self,
        text_prompt: str,
        image_b64: str,
    ) -> LLMResponse:
        """Send the screenshot + prompt to the vision model.

        Uses the provider's _call method directly with multimodal message format.
        """
        # Build multimodal messages (OpenAI format — also works with Anthropic adapter)
        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text_prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{image_b64}",
                            "detail": "high",
                        },
                    },
                ],
            },
        ]

        return await self.provider._call(messages)

    def _parse_vision_response(self, response: LLMResponse) -> ExtractionResult:
        """Parse the vision model's JSON response."""
        try:
            extracted, _, confidence = parse_extraction_json(response)
        except (json.JSONDecodeError, ValueError) as e:
            logger.error("Failed to parse vision response: %s", e)
            raise LLMProviderError(f"Vision model returned invalid JSON: {e}") from e

        return ExtractionResult(
            data=extracted,
            selectors={},  # Vision doesn't produce CSS selectors
            confidence=confidence,
            raw_response=response.parse_json() if hasattr(response, "parse_json") else {},
        )

    def _build_output_schema(
        self, fields: List[FieldDefinition | ListFieldDefinition]
    ) -> Dict[str, Any]:
        """Build the expected JSON output schema."""
        return {
            "data": build_data_schema(fields),
            "confidence": "<float 0.0-1.0>",
        }
