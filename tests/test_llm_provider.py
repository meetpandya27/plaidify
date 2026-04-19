"""Tests for the LLM extraction provider module."""

import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.llm_provider import (
    AnthropicProvider,
    FallbackChain,
    LLMAuthError,
    LLMBudgetExceededError,
    LLMProviderError,
    LLMRateLimitError,
    LLMResponse,
    OpenAIProvider,
    TokenUsage,
    create_provider,
)

# ── Data Classes ──────────────────────────────────────────────────────────────


class TestTokenUsage:
    def test_defaults(self):
        u = TokenUsage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0

    def test_values(self):
        u = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert u.prompt_tokens == 100
        assert u.total_tokens == 150

    def test_frozen(self):
        u = TokenUsage()
        with pytest.raises(FrozenInstanceError):
            u.prompt_tokens = 999


class TestLLMResponse:
    def test_basic(self):
        r = LLMResponse(
            content='{"balance": 100}',
            model="gpt-4o-mini",
            usage=TokenUsage(10, 5, 15),
            latency_ms=200.0,
            provider="openai",
        )
        assert r.content == '{"balance": 100}'
        assert r.model == "gpt-4o-mini"
        assert r.provider == "openai"
        assert r.raw == {}

    def test_parse_json_plain(self):
        r = LLMResponse(
            content='{"key": "value"}',
            model="m",
            usage=TokenUsage(),
            latency_ms=0,
            provider="test",
        )
        assert r.parse_json() == {"key": "value"}

    def test_parse_json_with_fences(self):
        r = LLMResponse(
            content='```json\n{"key": "value"}\n```',
            model="m",
            usage=TokenUsage(),
            latency_ms=0,
            provider="test",
        )
        assert r.parse_json() == {"key": "value"}

    def test_parse_json_with_plain_fences(self):
        r = LLMResponse(
            content='```\n{"a": 1}\n```',
            model="m",
            usage=TokenUsage(),
            latency_ms=0,
            provider="test",
        )
        assert r.parse_json() == {"a": 1}

    def test_parse_json_invalid(self):
        r = LLMResponse(
            content="not json",
            model="m",
            usage=TokenUsage(),
            latency_ms=0,
            provider="test",
        )
        with pytest.raises(json.JSONDecodeError):
            r.parse_json()

    def test_parse_json_with_whitespace(self):
        r = LLMResponse(
            content='  \n  {"ok": true}  \n  ',
            model="m",
            usage=TokenUsage(),
            latency_ms=0,
            provider="test",
        )
        assert r.parse_json() == {"ok": True}

    def test_frozen(self):
        r = LLMResponse(content="x", model="m", usage=TokenUsage(), latency_ms=0, provider="t")
        with pytest.raises(FrozenInstanceError):
            r.content = "y"


# ── Exceptions ────────────────────────────────────────────────────────────────


class TestExceptions:
    def test_base_error(self):
        e = LLMProviderError("fail")
        assert str(e) == "fail"

    def test_rate_limit_with_retry(self):
        e = LLMRateLimitError("rate limited", retry_after=30.0)
        assert e.retry_after == 30.0

    def test_rate_limit_no_retry(self):
        e = LLMRateLimitError("rate limited")
        assert e.retry_after is None

    def test_auth_error(self):
        e = LLMAuthError("bad key")
        assert isinstance(e, LLMProviderError)

    def test_budget_exceeded(self):
        e = LLMBudgetExceededError("too many tokens")
        assert isinstance(e, LLMProviderError)


# ── OpenAI Provider ───────────────────────────────────────────────────────────


def _mock_openai_response(
    content: str = '{"balance": 100}',
    model: str = "gpt-4o-mini",
    status_code: int = 200,
    prompt_tokens: int = 500,
    completion_tokens: int = 50,
):
    """Create a mock httpx response for OpenAI API."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    data = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "model": model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


class TestOpenAIProvider:
    def test_init_defaults(self):
        p = OpenAIProvider(api_key="sk-test")
        assert p.model == "gpt-4o-mini"
        assert p.provider_name == "openai"
        assert p.max_tokens == 4096
        assert p.temperature == 0.0
        assert p.base_url == "https://api.openai.com/v1"

    def test_init_custom(self):
        p = OpenAIProvider(
            model="gpt-4o",
            api_key="sk-test",
            base_url="https://custom.openai.azure.com/",
            max_tokens=8192,
            temperature=0.5,
        )
        assert p.model == "gpt-4o"
        assert p.base_url == "https://custom.openai.azure.com"
        assert p.max_tokens == 8192

    @pytest.mark.asyncio
    async def test_call_success(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        result = await p._call(
            [{"role": "user", "content": "extract data"}],
        )

        assert result.content == '{"balance": 100}'
        assert result.model == "gpt-4o-mini"
        assert result.provider == "openai"
        assert result.usage.prompt_tokens == 500
        assert result.usage.completion_tokens == 50
        assert result.latency_ms > 0

        # Verify correct payload
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/chat/completions"
        payload = call_args[1]["json"]
        assert payload["model"] == "gpt-4o-mini"
        assert payload["messages"] == [{"role": "user", "content": "extract data"}]

    @pytest.mark.asyncio
    async def test_call_with_json_mode(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p._call(
            [{"role": "user", "content": "test"}],
            response_format={"type": "json_object"},
        )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_call_rate_limit(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {"retry-after": "30"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMRateLimitError) as exc_info:
            await p._call([{"role": "user", "content": "test"}])
        assert exc_info.value.retry_after == 30.0

    @pytest.mark.asyncio
    async def test_call_auth_error_401(self):
        p = OpenAIProvider(api_key="bad-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMAuthError):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_auth_error_403(self):
        p = OpenAIProvider(api_key="bad-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMAuthError):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_server_error(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMProviderError, match="500"):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_overrides(self):
        p = OpenAIProvider(api_key="sk-test", max_tokens=4096, temperature=0.0)
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p._call(
            [{"role": "user", "content": "test"}],
            max_tokens=1024,
            temperature=0.7,
        )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["max_tokens"] == 1024
        assert payload["temperature"] == 0.7

    @pytest.mark.asyncio
    async def test_close(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_client = AsyncMock()
        p._client = mock_client

        await p.close()
        mock_client.aclose.assert_awaited_once()
        assert p._client is None

    @pytest.mark.asyncio
    async def test_close_no_client(self):
        p = OpenAIProvider(api_key="sk-test")
        await p.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_context_manager(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_client = AsyncMock()
        p._client = mock_client

        async with p:
            pass

        mock_client.aclose.assert_awaited_once()

    def test_get_client_creates_once(self):
        p = OpenAIProvider(api_key="sk-test")
        import httpx

        with patch.object(httpx, "AsyncClient") as mock_async:
            mock_async.return_value = MagicMock()
            c1 = p._get_client()
            c2 = p._get_client()
            assert c1 is c2
            mock_async.assert_called_once()


# ── Anthropic Provider ────────────────────────────────────────────────────────


def _mock_anthropic_response(
    content: str = '{"balance": 100}',
    model: str = "claude-sonnet-4-20250514",
    status_code: int = 200,
    input_tokens: int = 500,
    output_tokens: int = 50,
):
    """Create a mock httpx response for Anthropic API."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {}
    data = {
        "content": [{"type": "text", "text": content}],
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    resp.json.return_value = data
    resp.text = json.dumps(data)
    return resp


class TestAnthropicProvider:
    def test_init_defaults(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        assert p.model == "claude-sonnet-4-20250514"
        assert p.provider_name == "anthropic"
        assert p.base_url == "https://api.anthropic.com"

    @pytest.mark.asyncio
    async def test_call_success(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_resp = _mock_anthropic_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        result = await p._call(
            [{"role": "user", "content": "extract data"}],
        )

        assert result.content == '{"balance": 100}'
        assert result.provider == "anthropic"
        assert result.usage.prompt_tokens == 500
        assert result.usage.completion_tokens == 50
        assert result.usage.total_tokens == 550

        payload = mock_client.post.call_args[1]["json"]
        assert payload["model"] == "claude-sonnet-4-20250514"
        assert "system" not in payload

    @pytest.mark.asyncio
    async def test_call_with_system_prompt(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_resp = _mock_anthropic_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p._call(
            [
                {"role": "system", "content": "You are an extractor."},
                {"role": "user", "content": "extract data"},
            ],
        )

        payload = mock_client.post.call_args[1]["json"]
        assert payload["system"] == "You are an extractor."
        # System message should NOT be in messages array
        assert all(m["role"] != "system" for m in payload["messages"])

    @pytest.mark.asyncio
    async def test_call_rate_limit(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.headers = {}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMRateLimitError):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_auth_error(self):
        p = AnthropicProvider(api_key="bad-key")
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMAuthError):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_server_error(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal error"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        with pytest.raises(LLMProviderError, match="500"):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_close(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_client = AsyncMock()
        p._client = mock_client

        await p.close()
        mock_client.aclose.assert_awaited_once()
        assert p._client is None

    @pytest.mark.asyncio
    async def test_posts_to_correct_endpoint(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        mock_resp = _mock_anthropic_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p._call([{"role": "user", "content": "test"}])

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/messages"

    def test_get_client_headers(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        import httpx

        with patch.object(httpx, "AsyncClient") as mock_async:
            mock_async.return_value = MagicMock()
            p._get_client()
            call_kwargs = mock_async.call_args[1]
            assert call_kwargs["headers"]["x-api-key"] == "sk-ant-test"
            assert "anthropic-version" in call_kwargs["headers"]


# ── Extract Method (shared behavior) ─────────────────────────────────────────


class TestExtractMethod:
    @pytest.mark.asyncio
    async def test_extract_builds_messages(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p.extract("Extract balance", system_prompt="You are an extractor.")

        payload = mock_client.post.call_args[1]["json"]
        assert payload["messages"][0] == {
            "role": "system",
            "content": "You are an extractor.",
        }
        assert payload["messages"][1] == {
            "role": "user",
            "content": "Extract balance",
        }
        assert payload["response_format"] == {"type": "json_object"}

    @pytest.mark.asyncio
    async def test_extract_no_system_prompt(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p.extract("Extract balance")

        payload = mock_client.post.call_args[1]["json"]
        assert len(payload["messages"]) == 1
        assert payload["messages"][0]["role"] == "user"

    @pytest.mark.asyncio
    async def test_extract_no_json_mode(self):
        p = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response()
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p._client = mock_client

        await p.extract("Extract balance", json_mode=False)

        payload = mock_client.post.call_args[1]["json"]
        assert "response_format" not in payload


# ── Fallback Chain ────────────────────────────────────────────────────────────


class TestFallbackChain:
    def test_empty_providers_raises(self):
        with pytest.raises(ValueError, match="at least one"):
            FallbackChain([])

    def test_inherits_first_provider_config(self):
        p1 = OpenAIProvider(model="gpt-4o-mini", api_key="k", max_tokens=2048)
        p2 = OpenAIProvider(model="gpt-4o", api_key="k", max_tokens=8192)
        chain = FallbackChain([p1, p2])
        assert chain.model == "gpt-4o-mini"
        assert chain.max_tokens == 2048

    @pytest.mark.asyncio
    async def test_first_provider_succeeds(self):
        p1 = OpenAIProvider(api_key="sk-test")
        mock_resp = _mock_openai_response(model="gpt-4o-mini")
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        p1._client = mock_client

        p2 = OpenAIProvider(model="gpt-4o", api_key="sk-test")
        p2._client = AsyncMock()

        chain = FallbackChain([p1, p2])
        result = await chain._call([{"role": "user", "content": "test"}])

        assert result.model == "gpt-4o-mini"
        p2._client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_falls_back_on_error(self):
        p1 = OpenAIProvider(api_key="sk-test")
        p1_client = AsyncMock()
        p1_error_resp = MagicMock()
        p1_error_resp.status_code = 500
        p1_error_resp.text = "Server error"
        p1_client.post.return_value = p1_error_resp
        p1._client = p1_client

        p2 = OpenAIProvider(model="gpt-4o", api_key="sk-test")
        p2_client = AsyncMock()
        p2_client.post.return_value = _mock_openai_response(model="gpt-4o")
        p2._client = p2_client

        chain = FallbackChain([p1, p2])
        result = await chain._call([{"role": "user", "content": "test"}])

        assert result.model == "gpt-4o"

    @pytest.mark.asyncio
    async def test_auth_error_not_retried(self):
        p1 = OpenAIProvider(api_key="bad-key")
        p1_client = AsyncMock()
        p1_error_resp = MagicMock()
        p1_error_resp.status_code = 401
        p1_client.post.return_value = p1_error_resp
        p1._client = p1_client

        p2 = OpenAIProvider(model="gpt-4o", api_key="sk-test")
        p2._client = AsyncMock()

        chain = FallbackChain([p1, p2])
        with pytest.raises(LLMAuthError):
            await chain._call([{"role": "user", "content": "test"}])

        p2._client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_all_providers_fail(self):
        p1 = OpenAIProvider(api_key="sk-test")
        p1_client = AsyncMock()
        p1_resp = MagicMock()
        p1_resp.status_code = 500
        p1_resp.text = "err1"
        p1_client.post.return_value = p1_resp
        p1._client = p1_client

        p2 = OpenAIProvider(model="gpt-4o", api_key="sk-test")
        p2_client = AsyncMock()
        p2_resp = MagicMock()
        p2_resp.status_code = 503
        p2_resp.text = "err2"
        p2_client.post.return_value = p2_resp
        p2._client = p2_client

        chain = FallbackChain([p1, p2])
        with pytest.raises(LLMProviderError, match="All providers.*failed"):
            await chain._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_close_all_providers(self):
        p1 = OpenAIProvider(api_key="sk-test")
        p1._client = AsyncMock()
        p2 = AnthropicProvider(api_key="sk-ant-test")
        p2._client = AsyncMock()

        chain = FallbackChain([p1, p2])
        await chain.close()

        assert p1._client is None
        assert p2._client is None

    @pytest.mark.asyncio
    async def test_rate_limit_falls_back(self):
        p1 = OpenAIProvider(api_key="sk-test")
        p1_client = AsyncMock()
        p1_resp = MagicMock()
        p1_resp.status_code = 429
        p1_resp.headers = {}
        p1_client.post.return_value = p1_resp
        p1._client = p1_client

        p2 = OpenAIProvider(model="gpt-4o", api_key="sk-test")
        p2_client = AsyncMock()
        p2_client.post.return_value = _mock_openai_response(model="gpt-4o")
        p2._client = p2_client

        chain = FallbackChain([p1, p2])
        result = await chain._call([{"role": "user", "content": "test"}])
        assert result.model == "gpt-4o"


# ── Factory ───────────────────────────────────────────────────────────────────


class TestCreateProvider:
    def test_openai(self):
        p = create_provider("openai", api_key="sk-test")
        assert isinstance(p, OpenAIProvider)
        assert p.model == "gpt-4o-mini"

    def test_openai_custom_model(self):
        p = create_provider("openai", api_key="sk-test", model="gpt-4o")
        assert p.model == "gpt-4o"

    def test_anthropic(self):
        p = create_provider("anthropic", api_key="sk-ant-test")
        assert isinstance(p, AnthropicProvider)
        assert p.model == "claude-sonnet-4-20250514"

    def test_anthropic_custom_model(self):
        p = create_provider("anthropic", api_key="sk-ant-test", model="claude-opus-4-20250514")
        assert p.model == "claude-opus-4-20250514"

    def test_custom_base_url(self):
        p = create_provider(
            "openai",
            api_key="sk-test",
            base_url="https://my-azure.openai.azure.com",
        )
        assert isinstance(p, OpenAIProvider)
        assert p.base_url == "https://my-azure.openai.azure.com"

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            create_provider("bedrock", api_key="test")

    def test_case_insensitive(self):
        p = create_provider("  OpenAI  ", api_key="sk-test")
        assert isinstance(p, OpenAIProvider)

    def test_custom_params(self):
        p = create_provider(
            "openai",
            api_key="sk-test",
            max_tokens=8192,
            temperature=0.5,
            timeout=120.0,
        )
        assert p.max_tokens == 8192
        assert p.temperature == 0.5
        assert p.timeout == 120.0


# ── Config Integration ────────────────────────────────────────────────────────


class TestConfigIntegration:
    def test_llm_settings_defaults(self):
        """Verify LLM config fields exist with correct defaults."""
        import os

        os.environ.setdefault("ENCRYPTION_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
        os.environ.setdefault("JWT_SECRET_KEY", "testsecretkey1234567890abcdefghij")
        from src.config import Settings

        s = Settings()  # type: ignore[call-arg]
        assert s.llm_provider == "openai"
        assert s.llm_api_key is None
        assert s.llm_model is None
        assert s.llm_base_url is None
        assert s.llm_max_tokens == 4096
        assert s.llm_temperature == 0.0
        assert s.llm_timeout == 60.0
        assert s.llm_token_budget == 30000
        assert s.llm_fallback_model is None

    def test_llm_provider_validation(self):
        """Reject invalid provider names."""
        import os

        os.environ.setdefault("ENCRYPTION_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleXQ=")
        os.environ.setdefault("JWT_SECRET_KEY", "testsecretkey1234567890abcdefghij")
        from pydantic import ValidationError

        from src.config import Settings

        with pytest.raises(ValidationError, match="llm_provider"):
            Settings(llm_provider="bedrock")  # type: ignore[call-arg]
