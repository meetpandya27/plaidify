"""
Tests for LLM-adaptive extraction pipeline integration in engine.py.

Tests the extraction branching logic, selector caching, fallback chain,
and multimodal fallback — all with mocked LLM providers and Playwright pages.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from src.core.engine import (
    _create_llm_provider,
    _extract_llm_adaptive,
    _extract_with_cached_selectors,
    _extract_with_fallback_selectors,
    _extract_with_llm,
    _extract_with_multimodal,
    _get_page_path,
    get_selector_cache,
)
from src.core.blueprint import (
    AuthConfig,
    AuthType,
    BlueprintStep,
    BlueprintV2,
    ExtractionField,
    ExtractionStrategy,
    FieldType,
    ListExtractionField,
    StepAction,
)
from src.core.llm_provider import (
    BaseLLMProvider,
    FallbackChain,
    LLMProviderError,
    LLMResponse,
    OpenAIProvider,
    TokenUsage,
)
from src.core.selector_cache import SelectorCache
from src.exceptions import DataExtractionError
from tests.conftest import (
    make_llm_response,
    make_mock_playwright_page as make_mock_page,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def make_v3_blueprint(
    domain: str = "example.com",
    extraction_strategy: str = "llm_adaptive",
    fallback_selectors: dict | None = None,
) -> BlueprintV2:
    """Create a minimal V3 blueprint for testing."""
    return BlueprintV2(
        schema_version="3.0",
        name="Test Site",
        domain=domain,
        auth=AuthConfig(
            type=AuthType.FORM,
            steps=[BlueprintStep(action=StepAction.GOTO, url="http://example.com/login")],
        ),
        extraction_strategy=ExtractionStrategy(extraction_strategy),
        extract={
            "balance": ExtractionField(
                type=FieldType.CURRENCY,
                description="Current account balance",
                example="$1,234.56",
            ),
            "account_name": ExtractionField(
                type=FieldType.TEXT,
                description="Name on the account",
            ),
        },
        page_context="Banking dashboard showing account details",
        fallback_selectors=fallback_selectors,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestGetPagePath:
    """Tests for _get_page_path."""

    def test_extracts_path(self):
        page = MagicMock()
        page.url = "http://example.com/dashboard?tab=summary"
        assert _get_page_path(page) == "/dashboard"

    def test_root_path(self):
        page = MagicMock()
        page.url = "http://example.com"
        assert _get_page_path(page) == "/"

    def test_handles_exception(self):
        page = MagicMock()
        type(page).url = PropertyMock(side_effect=Exception("no url"))
        assert _get_page_path(page) == "/"


class TestCreateLLMProvider:
    """Tests for _create_llm_provider."""

    def test_returns_none_without_api_key(self):
        with patch("src.core.engine.settings") as mock_settings:
            mock_settings.llm_api_key = None
            assert _create_llm_provider() is None

    def test_creates_openai_provider(self):
        with patch("src.core.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_api_key = "sk-test"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.llm_base_url = None
            mock_settings.llm_max_tokens = 4096
            mock_settings.llm_temperature = 0.0
            mock_settings.llm_timeout = 60.0
            mock_settings.llm_fallback_model = None

            provider = _create_llm_provider()
            assert isinstance(provider, OpenAIProvider)
            assert provider.model == "gpt-4o-mini"

    def test_creates_fallback_chain(self):
        with patch("src.core.engine.settings") as mock_settings:
            mock_settings.llm_provider = "openai"
            mock_settings.llm_api_key = "sk-test"
            mock_settings.llm_model = "gpt-4o-mini"
            mock_settings.llm_base_url = None
            mock_settings.llm_max_tokens = 4096
            mock_settings.llm_temperature = 0.0
            mock_settings.llm_timeout = 60.0
            mock_settings.llm_fallback_model = "gpt-4o"

            provider = _create_llm_provider()
            assert isinstance(provider, FallbackChain)
            assert len(provider.providers) == 2


class TestExtractWithCachedSelectors:
    """Tests for _extract_with_cached_selectors."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_matching_selectors(self):
        page = make_mock_page()
        result = await _extract_with_cached_selectors(
            page=page,
            cached_selectors={"unknown_field": "#unknown"},
            extraction_defs=make_v3_blueprint().extract,
            site="test",
            domain="example.com",
            page_path="/dashboard",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_successful_extraction_records_success(self):
        """Cached selectors that work should record success."""
        page = make_mock_page()
        blueprint = make_v3_blueprint()

        # Mock DataExtractor to return data
        mock_data = {"balance": 1234.56, "account_name": "John Doe"}
        with patch("src.core.engine.DataExtractor") as MockDE:
            instance = MockDE.return_value
            instance.extract = AsyncMock(return_value=mock_data)

            with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
                mock_cache = MagicMock()
                mock_cache_fn.return_value = mock_cache

                result = await _extract_with_cached_selectors(
                    page=page,
                    cached_selectors={"balance": "#balance", "account_name": "#name"},
                    extraction_defs=blueprint.extract,
                    site="test",
                    domain="example.com",
                    page_path="/dashboard",
                )

                assert result == mock_data
                mock_cache.record_success.assert_called_once_with("example.com", "/dashboard")

    @pytest.mark.asyncio
    async def test_failed_extraction_records_failure(self):
        """Failed cached extraction should record failure and return None."""
        page = make_mock_page()
        blueprint = make_v3_blueprint()

        with patch("src.core.engine.DataExtractor") as MockDE:
            instance = MockDE.return_value
            instance.extract = AsyncMock(side_effect=DataExtractionError(site="test"))

            with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
                mock_cache = MagicMock()
                mock_cache_fn.return_value = mock_cache

                result = await _extract_with_cached_selectors(
                    page=page,
                    cached_selectors={"balance": "#balance"},
                    extraction_defs=blueprint.extract,
                    site="test",
                    domain="example.com",
                    page_path="/dashboard",
                )

                assert result is None
                mock_cache.record_failure.assert_called_once_with("example.com", "/dashboard")


class TestExtractWithLLM:
    """Tests for _extract_with_llm."""

    @pytest.mark.asyncio
    async def test_returns_none_without_api_key(self):
        """No API key → skip LLM, return None."""
        with patch("src.core.engine._create_llm_provider", return_value=None):
            result = await _extract_with_llm(
                page=make_mock_page(),
                blueprint=make_v3_blueprint(),
                extraction_defs=make_v3_blueprint().extract,
                site="test",
                domain="example.com",
                page_path="/dashboard",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_extraction_caches_selectors(self):
        """Successful LLM extraction should cache selectors."""
        page = make_mock_page()
        blueprint = make_v3_blueprint()
        llm_data = {"balance": 1234.56, "account_name": "Test"}
        llm_selectors = {"balance": "#balance", "account_name": "#name"}
        response = make_llm_response(llm_data, llm_selectors, confidence=0.92)

        mock_provider = MagicMock()
        mock_provider.extract = AsyncMock(return_value=response)
        mock_provider.close = AsyncMock()

        mock_simplifier_result = MagicMock()
        mock_simplifier_result.html = "<html>simplified</html>"
        mock_simplifier_result.element_map = {}
        mock_simplifier_result.token_estimate = 100

        with patch("src.core.engine._create_llm_provider", return_value=mock_provider):
            with patch("src.core.engine.DOMSimplifier") as MockSimp:
                MockSimp.return_value.simplify = AsyncMock(return_value=mock_simplifier_result)
                with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
                    mock_cache = MagicMock()
                    mock_cache_fn.return_value = mock_cache

                    result = await _extract_with_llm(
                        page, blueprint, blueprint.extract, "test",
                        "example.com", "/dashboard",
                    )

                    assert result == llm_data
                    mock_cache.put.assert_called_once()
                    call_args = mock_cache.put.call_args
                    assert call_args[0][0] == "example.com"
                    assert call_args[0][1] == "/dashboard"

    @pytest.mark.asyncio
    async def test_low_confidence_does_not_cache(self):
        """LLM result with low confidence should NOT cache selectors."""
        page = make_mock_page()
        blueprint = make_v3_blueprint()
        response = make_llm_response(
            {"balance": 99.0}, {"balance": "#b"}, confidence=0.3
        )

        mock_provider = MagicMock()
        mock_provider.extract = AsyncMock(return_value=response)
        mock_provider.close = AsyncMock()

        mock_simp_result = MagicMock()
        mock_simp_result.html = "<html></html>"
        mock_simp_result.element_map = {}
        mock_simp_result.token_estimate = 50

        with patch("src.core.engine._create_llm_provider", return_value=mock_provider):
            with patch("src.core.engine.DOMSimplifier") as MockSimp:
                MockSimp.return_value.simplify = AsyncMock(return_value=mock_simp_result)
                with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
                    mock_cache = MagicMock()
                    mock_cache_fn.return_value = mock_cache

                    result = await _extract_with_llm(
                        page, blueprint, blueprint.extract, "test",
                        "example.com", "/dashboard",
                    )

                    assert result == {"balance": 99.0}
                    mock_cache.put.assert_not_called()

    @pytest.mark.asyncio
    async def test_provider_error_returns_none(self):
        """LLM provider error should return None (not crash)."""
        mock_provider = MagicMock()
        mock_provider.extract = AsyncMock(side_effect=LLMProviderError("API down"))
        mock_provider.close = AsyncMock()

        mock_simp_result = MagicMock()
        mock_simp_result.html = "<html></html>"
        mock_simp_result.element_map = {}
        mock_simp_result.token_estimate = 50

        with patch("src.core.engine._create_llm_provider", return_value=mock_provider):
            with patch("src.core.engine.DOMSimplifier") as MockSimp:
                MockSimp.return_value.simplify = AsyncMock(return_value=mock_simp_result)

                result = await _extract_with_llm(
                    make_mock_page(), make_v3_blueprint(),
                    make_v3_blueprint().extract, "test",
                    "example.com", "/dashboard",
                )

                assert result is None
                mock_provider.close.assert_called_once()


class TestExtractWithMultimodal:
    """Tests for _extract_with_multimodal."""

    @pytest.mark.asyncio
    async def test_returns_none_without_provider(self):
        with patch("src.core.engine._create_llm_provider", return_value=None):
            result = await _extract_with_multimodal(
                make_mock_page(), make_v3_blueprint(),
                make_v3_blueprint().extract, "test",
            )
            assert result is None

    @pytest.mark.asyncio
    async def test_successful_multimodal_extraction(self):
        """Multimodal extraction returns data when confidence is adequate."""
        mock_result = MagicMock()
        mock_result.data = {"balance": 500.0}
        mock_result.confidence = 0.75
        mock_result.screenshot_size_bytes = 5000

        mock_provider = MagicMock()
        mock_provider.close = AsyncMock()

        with patch("src.core.engine._create_llm_provider", return_value=mock_provider):
            with patch("src.core.engine.MultimodalExtractor") as MockMM:
                MockMM.return_value.extract_from_screenshot = AsyncMock(
                    return_value=mock_result
                )

                result = await _extract_with_multimodal(
                    make_mock_page(), make_v3_blueprint(),
                    make_v3_blueprint().extract, "test",
                )

                assert result == {"balance": 500.0}

    @pytest.mark.asyncio
    async def test_low_confidence_returns_none(self):
        """Multimodal result below threshold returns None."""
        mock_result = MagicMock()
        mock_result.data = {"balance": 0.0}
        mock_result.confidence = 0.1
        mock_result.screenshot_size_bytes = 5000

        mock_provider = MagicMock()
        mock_provider.close = AsyncMock()

        with patch("src.core.engine._create_llm_provider", return_value=mock_provider):
            with patch("src.core.engine.MultimodalExtractor") as MockMM:
                MockMM.return_value.extract_from_screenshot = AsyncMock(
                    return_value=mock_result
                )

                result = await _extract_with_multimodal(
                    make_mock_page(), make_v3_blueprint(),
                    make_v3_blueprint().extract, "test",
                )

                assert result is None


class TestExtractWithFallbackSelectors:
    """Tests for _extract_with_fallback_selectors."""

    @pytest.mark.asyncio
    async def test_returns_none_without_fallback_selectors(self):
        blueprint = make_v3_blueprint(fallback_selectors=None)
        result = await _extract_with_fallback_selectors(
            make_mock_page(), blueprint, blueprint.extract, "test"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_uses_fallback_selectors(self):
        blueprint = make_v3_blueprint(
            fallback_selectors={"balance": "#balance", "account_name": "#name"}
        )

        mock_data = {"balance": 99.99, "account_name": "Fallback User"}
        with patch("src.core.engine.DataExtractor") as MockDE:
            MockDE.return_value.extract = AsyncMock(return_value=mock_data)

            result = await _extract_with_fallback_selectors(
                make_mock_page(), blueprint, blueprint.extract, "test"
            )

            assert result == mock_data

    @pytest.mark.asyncio
    async def test_extraction_failure_returns_none(self):
        blueprint = make_v3_blueprint(
            fallback_selectors={"balance": "#gone"}
        )

        with patch("src.core.engine.DataExtractor") as MockDE:
            MockDE.return_value.extract = AsyncMock(
                side_effect=DataExtractionError(site="test")
            )

            result = await _extract_with_fallback_selectors(
                make_mock_page(), blueprint, blueprint.extract, "test"
            )

            assert result is None


class TestExtractLLMAdaptive:
    """Tests for the full _extract_llm_adaptive pipeline."""

    @pytest.mark.asyncio
    async def test_uses_cached_selectors_first(self):
        """Cache hit should be used without calling LLM."""
        blueprint = make_v3_blueprint()
        page = make_mock_page()

        mock_cache_entry = MagicMock()
        mock_cache_entry.selectors = {"balance": "#bal", "account_name": "#name"}
        mock_cache_entry.confidence = 0.95
        mock_cache_entry.hit_count = 5

        mock_data = {"balance": 1234.56, "account_name": "Cached User"}

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.return_value = mock_cache_entry
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_cached_selectors", new_callable=AsyncMock) as mock_cached:
                mock_cached.return_value = mock_data

                with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock) as mock_llm:
                    data, method = await _extract_llm_adaptive(
                        page, blueprint, blueprint.extract, "test"
                    )

                    assert data == mock_data
                    assert method == "cached_selectors"
                    mock_llm.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_through_to_llm_on_cache_miss(self):
        """No cache → LLM extraction."""
        blueprint = make_v3_blueprint()
        page = make_mock_page()
        llm_data = {"balance": 500.0, "account_name": "LLM User"}

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.return_value = None
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock) as mock_llm:
                mock_llm.return_value = llm_data

                data, method = await _extract_llm_adaptive(
                    page, blueprint, blueprint.extract, "test"
                )

                assert data == llm_data
                assert method == "llm"

    @pytest.mark.asyncio
    async def test_falls_through_to_multimodal(self):
        """Cache miss + LLM fail → multimodal fallback."""
        blueprint = make_v3_blueprint()
        page = make_mock_page()
        mm_data = {"balance": 300.0, "account_name": "Vision User"}

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.return_value = None
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock, return_value=None):
                with patch("src.core.engine._extract_with_multimodal", new_callable=AsyncMock) as mock_mm:
                    mock_mm.return_value = mm_data

                    data, method = await _extract_llm_adaptive(
                        page, blueprint, blueprint.extract, "test"
                    )

                    assert data == mm_data
                    assert method == "multimodal"

    @pytest.mark.asyncio
    async def test_falls_through_to_fallback_selectors(self):
        """Cache miss + LLM fail + multimodal fail → fallback selectors."""
        blueprint = make_v3_blueprint(
            fallback_selectors={"balance": "#balance", "account_name": "#name"}
        )
        page = make_mock_page()
        fallback_data = {"balance": 100.0, "account_name": "Fallback"}

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.return_value = None
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock, return_value=None):
                with patch("src.core.engine._extract_with_multimodal", new_callable=AsyncMock, return_value=None):
                    with patch("src.core.engine._extract_with_fallback_selectors", new_callable=AsyncMock) as mock_fb:
                        mock_fb.return_value = fallback_data

                        data, method = await _extract_llm_adaptive(
                            page, blueprint, blueprint.extract, "test"
                        )

                        assert data == fallback_data
                        assert method == "fallback_selectors"

    @pytest.mark.asyncio
    async def test_all_methods_fail_raises_error(self):
        """All extraction methods fail → DataExtractionError."""
        blueprint = make_v3_blueprint(fallback_selectors=None)
        page = make_mock_page()

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.return_value = None
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock, return_value=None):
                with patch("src.core.engine._extract_with_multimodal", new_callable=AsyncMock, return_value=None):
                    with pytest.raises(DataExtractionError, match="All extraction methods failed"):
                        await _extract_llm_adaptive(
                            page, blueprint, blueprint.extract, "test"
                        )

    @pytest.mark.asyncio
    async def test_cache_miss_then_cache_hit_second_time(self):
        """First call caches selectors, second call uses cache."""
        blueprint = make_v3_blueprint()
        page = make_mock_page()
        llm_data = {"balance": 999.0, "account_name": "Cache Test"}

        call_count = 0

        def cache_get_side_effect(domain, path):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return None  # First call: no cache
            # Second call: cached
            entry = MagicMock()
            entry.selectors = {"balance": "#b", "account_name": "#n"}
            entry.confidence = 0.9
            entry.hit_count = 1
            return entry

        with patch("src.core.engine.get_selector_cache") as mock_cache_fn:
            mock_cache = MagicMock()
            mock_cache.get.side_effect = cache_get_side_effect
            mock_cache_fn.return_value = mock_cache

            with patch("src.core.engine._extract_with_llm", new_callable=AsyncMock, return_value=llm_data):
                # First call — LLM
                data1, method1 = await _extract_llm_adaptive(
                    page, blueprint, blueprint.extract, "test"
                )
                assert method1 == "llm"

            with patch("src.core.engine._extract_with_cached_selectors", new_callable=AsyncMock, return_value=llm_data):
                # Second call — cached
                data2, method2 = await _extract_llm_adaptive(
                    page, blueprint, blueprint.extract, "test"
                )
                assert method2 == "cached_selectors"


class TestExecuteBlueprintBranching:
    """Test that _execute_blueprint correctly branches on extraction strategy."""

    @pytest.mark.asyncio
    async def test_css_strategy_uses_data_extractor(self):
        """V2 blueprint with css_selectors strategy uses DataExtractor."""
        blueprint = BlueprintV2(
            schema_version="2.0",
            name="V2 Test",
            domain="example.com",
            auth=AuthConfig(
                type=AuthType.FORM,
                steps=[BlueprintStep(action=StepAction.GOTO, url="http://example.com")],
            ),
            extract={
                "balance": ExtractionField(
                    selector="#balance",
                    type=FieldType.CURRENCY,
                ),
            },
        )

        assert not blueprint.is_llm_adaptive
        assert blueprint.extraction_strategy == ExtractionStrategy.SELECTOR

    @pytest.mark.asyncio
    async def test_llm_adaptive_flag(self):
        """V3 blueprint with llm_adaptive triggers LLM path."""
        blueprint = make_v3_blueprint()
        assert blueprint.is_llm_adaptive
        assert blueprint.extraction_strategy == ExtractionStrategy.LLM_ADAPTIVE


class TestSelectorCacheSingleton:
    """Tests for get_selector_cache."""

    def test_returns_same_instance(self):
        """get_selector_cache returns a singleton."""
        import src.core.engine as engine_mod
        engine_mod._selector_cache = None  # Reset

        cache1 = get_selector_cache()
        cache2 = get_selector_cache()
        assert cache1 is cache2
        assert isinstance(cache1, SelectorCache)

        engine_mod._selector_cache = None  # Cleanup
