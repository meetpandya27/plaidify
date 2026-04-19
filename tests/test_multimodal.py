"""Tests for multimodal (screenshot-based) extraction."""

import base64
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.extraction_prompt import (
    FieldDefinition,
    ListFieldDefinition,
)
from src.core.llm_provider import (
    BaseLLMProvider,
    LLMProviderError,
    LLMResponse,
    TokenUsage,
)
from src.core.multimodal_extractor import (
    VISION_SYSTEM_PROMPT,
    MultimodalExtractionResult,
    MultimodalExtractor,
)
from tests.conftest import (
    FAKE_SCREENSHOT,
)
from tests.conftest import (
    make_mock_llm_provider as make_mock_provider,
)
from tests.conftest import (
    make_mock_playwright_page as make_mock_page,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


SAMPLE_FIELDS = [
    FieldDefinition(name="account_number", type="text", description="Account ID"),
    FieldDefinition(name="balance", type="currency", description="Current balance", example="$142.50"),
]

SAMPLE_LIST_FIELDS = [
    FieldDefinition(name="account_number", type="text", description="Account ID"),
    ListFieldDefinition(
        name="transactions",
        description="Recent transaction list",
        fields=(
            FieldDefinition(name="date", type="date"),
            FieldDefinition(name="amount", type="currency"),
        ),
    ),
]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestMultimodalExtractor:
    """Tests for MultimodalExtractor."""

    @pytest.mark.asyncio
    async def test_basic_extraction(self):
        """Extract fields from a screenshot with good confidence."""
        response_data = {
            "data": {"account_number": "ACC-12345", "balance": 142.50},
            "confidence": 0.92,
        }
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        assert isinstance(result, MultimodalExtractionResult)
        assert result.data["account_number"] == "ACC-12345"
        assert result.data["balance"] == 142.50
        assert result.confidence == 0.92
        assert result.screenshot_size_bytes == len(FAKE_SCREENSHOT)
        assert result.latency_ms > 0

        # Provider _call was invoked with multimodal messages
        provider._call.assert_called_once()
        call_args = provider._call.call_args
        messages = call_args[0][0]
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == VISION_SYSTEM_PROMPT
        assert messages[1]["role"] == "user"
        # User message has text + image parts
        content = messages[1]["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    @pytest.mark.asyncio
    async def test_list_field_extraction(self):
        """Extract list/table fields from screenshot."""
        response_data = {
            "data": {
                "account_number": "ACC-999",
                "transactions": [
                    {"date": "2024-01-15", "amount": 42.00},
                    {"date": "2024-01-20", "amount": 89.50},
                ],
            },
            "confidence": 0.85,
        }
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(page, SAMPLE_LIST_FIELDS)

        assert result.data["account_number"] == "ACC-999"
        assert len(result.data["transactions"]) == 2
        assert result.data["transactions"][0]["amount"] == 42.00

    @pytest.mark.asyncio
    async def test_page_context_included_in_prompt(self):
        """Page context is passed to the vision model."""
        response_data = {"data": {"account_number": "X"}, "confidence": 0.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        await extractor.extract_from_screenshot(page, SAMPLE_FIELDS, page_context="Utility bill dashboard")

        call_args = provider._call.call_args
        messages = call_args[0][0]
        text_content = messages[1]["content"][0]["text"]
        assert "Utility bill dashboard" in text_content

    @pytest.mark.asyncio
    async def test_full_page_screenshot(self):
        """full_page flag is passed to playwright screenshot."""
        response_data = {"data": {}, "confidence": 0.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        await extractor.extract_from_screenshot(page, SAMPLE_FIELDS, full_page=True)

        page.screenshot.assert_called_once_with(type="png", full_page=True)

    @pytest.mark.asyncio
    async def test_viewport_not_resized_if_within_limits(self):
        """Viewport is not changed if already within max dimensions."""
        response_data = {"data": {}, "confidence": 0.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page(viewport_width=1024, viewport_height=768)

        extractor = MultimodalExtractor(provider)
        await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        page.set_viewport_size.assert_not_called()

    @pytest.mark.asyncio
    async def test_viewport_resized_if_exceeds_limits(self):
        """Viewport is resized if it exceeds max dimensions."""
        response_data = {"data": {}, "confidence": 0.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page(viewport_width=1920, viewport_height=1200)

        extractor = MultimodalExtractor(provider, max_width=1280, max_height=1024)
        await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        page.set_viewport_size.assert_called_once_with({"width": 1280, "height": 1024})

    @pytest.mark.asyncio
    async def test_low_confidence_result(self):
        """Low confidence result is still returned (threshold checked by caller)."""
        response_data = {"data": {"account_number": "maybe"}, "confidence": 0.15}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider, confidence_threshold=0.3)
        result = await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        assert result.confidence == 0.15
        assert result.data["account_number"] == "maybe"

    @pytest.mark.asyncio
    async def test_missing_confidence_defaults_to_zero(self):
        """Missing confidence field defaults to 0.0."""
        response_data = {"data": {"account_number": "X"}}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_invalid_json_raises_error(self):
        """Invalid JSON from the model raises LLMProviderError."""
        provider = MagicMock(spec=BaseLLMProvider)
        response = LLMResponse(
            content="NOT JSON AT ALL",
            model="mock-vision",
            usage=TokenUsage(),
            latency_ms=100,
            provider="mock",
        )
        provider._call = AsyncMock(return_value=response)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        with pytest.raises(LLMProviderError, match="invalid JSON"):
            await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

    @pytest.mark.asyncio
    async def test_non_dict_response_raises_error(self):
        """Non-dict JSON response raises LLMProviderError."""
        provider = MagicMock(spec=BaseLLMProvider)
        response = LLMResponse(
            content="[1, 2, 3]",
            model="mock-vision",
            usage=TokenUsage(),
            latency_ms=100,
            provider="mock",
        )
        provider._call = AsyncMock(return_value=response)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        with pytest.raises(LLMProviderError, match="Expected dict"):
            await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

    @pytest.mark.asyncio
    async def test_provider_error_propagates(self):
        """LLMProviderError from the model propagates."""
        provider = MagicMock(spec=BaseLLMProvider)
        provider._call = AsyncMock(side_effect=LLMProviderError("Vision API down"))
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        with pytest.raises(LLMProviderError, match="Vision API down"):
            await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

    @pytest.mark.asyncio
    async def test_screenshot_base64_encoded_correctly(self):
        """Screenshot bytes are properly base64-encoded in the message."""
        response_data = {"data": {}, "confidence": 0.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        call_args = provider._call.call_args
        messages = call_args[0][0]
        image_url = messages[1]["content"][1]["image_url"]["url"]
        # Extract the base64 part and verify it decodes back to our screenshot
        b64_data = image_url.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert decoded == FAKE_SCREENSHOT

    @pytest.mark.asyncio
    async def test_confidence_clamped_to_valid_range(self):
        """Confidence values > 1.0 are clamped."""
        response_data = {"data": {}, "confidence": 1.5}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        assert result.confidence == 1.0

    @pytest.mark.asyncio
    async def test_token_usage_tracked(self):
        """Token usage from the vision call is recorded in the result."""
        response_data = {"data": {"account_number": "X"}, "confidence": 0.9}
        provider = make_mock_provider(response_data)
        page = make_mock_page()

        extractor = MultimodalExtractor(provider)
        result = await extractor.extract_from_screenshot(page, SAMPLE_FIELDS)

        assert result.token_usage.prompt_tokens == 500
        assert result.token_usage.completion_tokens == 200
        assert result.token_usage.total_tokens == 700


class TestVisionPromptBuilding:
    """Tests for the vision prompt construction."""

    def test_build_output_schema_scalar_fields(self):
        """Output schema includes correct type hints for scalar fields."""
        extractor = MultimodalExtractor(MagicMock())
        schema = extractor._build_output_schema(SAMPLE_FIELDS)

        assert "data" in schema
        assert "confidence" in schema
        assert schema["data"]["account_number"] == "<text>"
        assert schema["data"]["balance"] == "<number>"

    def test_build_output_schema_list_fields(self):
        """Output schema handles list fields correctly."""
        extractor = MultimodalExtractor(MagicMock())
        schema = extractor._build_output_schema(SAMPLE_LIST_FIELDS)

        assert isinstance(schema["data"]["transactions"], list)
        assert "date" in schema["data"]["transactions"][0]
        assert "amount" in schema["data"]["transactions"][0]

    def test_vision_prompt_contains_field_specs(self):
        """Vision prompt includes field definitions."""
        extractor = MultimodalExtractor(MagicMock())
        prompt = extractor._build_vision_prompt(SAMPLE_FIELDS)

        assert "account_number" in prompt
        assert "Account ID" in prompt
        assert "balance" in prompt
        assert "$142.50" in prompt

    def test_vision_prompt_with_page_context(self):
        """Vision prompt includes page context when provided."""
        extractor = MultimodalExtractor(MagicMock())
        prompt = extractor._build_vision_prompt(SAMPLE_FIELDS, page_context="Energy bill dashboard")

        assert "## Page Context" in prompt
        assert "Energy bill dashboard" in prompt

    def test_vision_prompt_without_page_context(self):
        """Vision prompt omits page context section when not provided."""
        extractor = MultimodalExtractor(MagicMock())
        prompt = extractor._build_vision_prompt(SAMPLE_FIELDS)

        assert "## Page Context" not in prompt
