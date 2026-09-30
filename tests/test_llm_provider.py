"""Tests for the LLM extraction provider module."""

import json
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx2
import pytest

from src.core.llm_provider import (
    ANTHROPIC_FALLBACK_BETA,
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
    json_schema_format,
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
        assert p.model == "gpt-5.4-mini"
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
        p = OpenAIProvider(model="gpt-4o-mini", api_key="sk-test")
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
        p = OpenAIProvider(model="gpt-4o-mini", api_key="sk-test", max_tokens=4096, temperature=0.0)
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
#
# These run the real anthropic SDK. Only its transport is swapped, for an
# httpx2.MockTransport that records each request and answers from a queue.

_SSE_HEADERS = {"content-type": "text/event-stream"}


class _MessagesAPI:
    """Stands in for the Messages API: records requests and answers with queued replies."""

    def __init__(self):
        self.replies = []
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def body(self, index=-1):
        return json.loads(self.requests[index].content)


@pytest.fixture
def anthropic_api(monkeypatch):
    """Every SDK client an AnthropicProvider builds during the test talks to one _MessagesAPI."""
    api = _MessagesAPI()
    sdk_client = anthropic.AsyncAnthropic

    def client(**kwargs):
        transport = httpx2.MockTransport(api.handle)
        return sdk_client(http_client=anthropic.DefaultAsyncHttpxClient(transport=transport), **kwargs)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", client)
    return api


def _sse_body(*events):
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _sse(*events):
    """A 200 reply that streams ``events`` as server-sent events."""
    return httpx2.Response(200, headers=_SSE_HEADERS, content=_sse_body(*events))


def _message_events(text='{"ok": true}', stop_reason="end_turn", model="claude-opus-5"):
    """The events of one streamed message: a thinking block (which the provider skips), then ``text``."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_test",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 500, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "c2ln"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": text}},
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": 50},
        },
        {"type": "message_stop"},
    ]


def _anthropic_reply(text='{"ok": true}', **kwargs):
    return _sse(*_message_events(text, **kwargs))


def _anthropic_error(status_code, error_type, message, headers=None):
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    return httpx2.Response(status_code, headers=headers, json=body)


class _FailingStream(httpx2.AsyncByteStream):
    """A response body that raises ``error`` after its first chunk."""

    def __init__(self, first_chunk, error):
        self.first_chunk = first_chunk
        self.error = error

    async def __aiter__(self):
        yield self.first_chunk
        raise self.error


def _broken_stream(error):
    """A 200 reply whose stream breaks with ``error`` partway through the message."""
    return httpx2.Response(200, headers=_SSE_HEADERS, stream=_FailingStream(_sse_body(*_message_events()[:3]), error))


class TestAnthropicProvider:
    def test_init_defaults(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        assert p.model == "claude-opus-5"
        assert p.provider_name == "anthropic"
        assert p.base_url == "https://api.anthropic.com"

    @pytest.mark.asyncio
    async def test_call_success(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_reply('{"balance": 100}'))
        p = AnthropicProvider(api_key="sk-ant-test")

        result = await p._call([{"role": "user", "content": "extract data"}])

        assert result.content == '{"balance": 100}'
        assert result.model == "claude-opus-5"
        assert result.provider == "anthropic"
        assert result.usage == TokenUsage(prompt_tokens=500, completion_tokens=50, total_tokens=550)
        assert result.latency_ms > 0
        assert result.raw["stop_reason"] == "end_turn"
        assert [block["type"] for block in result.raw["content"]] == ["thinking", "text"]

        request = anthropic_api.requests[0]
        assert request.headers["x-api-key"] == "sk-ant-test"
        assert request.headers["anthropic-version"] == "2023-06-01"
        body = anthropic_api.body()
        assert body["model"] == "claude-opus-5"
        assert body["messages"] == [{"role": "user", "content": "extract data"}]
        assert body["max_tokens"] == 4096
        assert body["stream"] is True
        assert "system" not in body
        # Current Claude models reject sampling parameters.
        assert "temperature" not in body

    @pytest.mark.asyncio
    async def test_extract_sends_the_system_prompt_as_system(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_reply())
        p = AnthropicProvider(api_key="sk-ant-test")

        result = await p.extract("extract data", system_prompt="You are an extractor.")

        assert result.parse_json() == {"ok": True}
        body = anthropic_api.body()
        assert body["system"] == "You are an extractor."
        # System message should NOT be in messages array
        assert body["messages"] == [{"role": "user", "content": "extract data"}]

    @pytest.mark.asyncio
    async def test_call_rate_limit(self, anthropic_api):
        anthropic_api.replies.append(
            _anthropic_error(429, "rate_limit_error", "Too many requests", headers={"retry-after": "30"})
        )
        p = AnthropicProvider(api_key="sk-ant-test")

        with pytest.raises(LLMRateLimitError, match="rate limit") as caught:
            await p._call([{"role": "user", "content": "test"}])
        assert caught.value.retry_after == 30.0
        # The SDK does not retry by itself; the caller (FallbackChain) decides.
        assert len(anthropic_api.requests) == 1

    @pytest.mark.asyncio
    async def test_overloaded_is_retryable(self, anthropic_api):
        anthropic_api.replies.append(
            _anthropic_error(529, "overloaded_error", "Overloaded", headers={"retry-after": "7"})
        )
        p = AnthropicProvider(api_key="sk-ant-test")

        with pytest.raises(LLMRateLimitError, match="overloaded") as caught:
            await p._call([{"role": "user", "content": "test"}])
        assert caught.value.retry_after == 7.0
        assert len(anthropic_api.requests) == 1

    @pytest.mark.asyncio
    async def test_overload_during_the_stream_is_retryable(self, anthropic_api):
        overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
        anthropic_api.replies.append(_sse(*_message_events()[:5], overloaded))
        p = AnthropicProvider(api_key="sk-ant-test")

        with pytest.raises(LLMRateLimitError, match="overloaded"):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code, error_type", [(401, "authentication_error"), (403, "permission_error")])
    async def test_call_auth_error(self, anthropic_api, status_code, error_type):
        anthropic_api.replies.append(_anthropic_error(status_code, error_type, "invalid x-api-key"))
        p = AnthropicProvider(api_key="bad-key")

        with pytest.raises(LLMAuthError, match=str(status_code)):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_call_server_error(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_error(500, "api_error", "Internal server error"))
        p = AnthropicProvider(api_key="sk-ant-test")

        with pytest.raises(LLMProviderError, match="500: Internal server error"):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_timeout(self, anthropic_api):
        anthropic_api.replies.append(httpx2.ReadTimeout("timed out"))
        p = AnthropicProvider(api_key="sk-ant-test", timeout=12.5)

        with pytest.raises(LLMProviderError, match="timed out after 12.5s"):
            await p._call([{"role": "user", "content": "test"}])
        timeouts = anthropic_api.requests[0].extensions["timeout"]
        assert timeouts == {"connect": 12.5, "read": 12.5, "write": 12.5, "pool": 12.5}

    @pytest.mark.asyncio
    async def test_connection_error(self, anthropic_api):
        anthropic_api.replies.append(httpx2.ConnectError("connection refused"))
        p = AnthropicProvider(api_key="sk-ant-test")

        with pytest.raises(LLMProviderError, match="request failed: ConnectError"):
            await p._call([{"role": "user", "content": "test"}])

    @pytest.mark.asyncio
    async def test_close(self, anthropic_api):
        p = AnthropicProvider(api_key="sk-ant-test")
        client = p._get_client()

        await p.close()

        assert client.is_closed()
        assert p._client is None

    @pytest.mark.asyncio
    async def test_close_no_client(self):
        p = AnthropicProvider(api_key="sk-ant-test")
        await p.close()  # Should not raise


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
    async def test_close_all_providers(self, anthropic_api):
        p1 = OpenAIProvider(api_key="sk-test")
        p1._client = AsyncMock()
        p2 = AnthropicProvider(api_key="sk-ant-test")
        sdk_client = p2._get_client()

        chain = FallbackChain([p1, p2])
        await chain.close()

        assert p1._client is None
        assert p2._client is None
        assert sdk_client.is_closed()

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
        assert p.model == "gpt-5.4-mini"

    def test_openai_custom_model(self):
        p = create_provider("openai", api_key="sk-test", model="gpt-4o")
        assert p.model == "gpt-4o"

    def test_anthropic(self):
        p = create_provider("anthropic", api_key="sk-ant-test")
        assert isinstance(p, AnthropicProvider)
        assert p.model == "claude-opus-5"

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


# ── Request shaping and failure wrapping (ENG-07) ────────────────────────────


def _client_returning(*responses):
    client = AsyncMock()
    client.post.side_effect = list(responses)
    return client


def _json_response(status_code, body, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = body
    resp.text = json.dumps(body)
    return resp


class TestOpenAIRequestShape:
    @pytest.mark.asyncio
    async def test_reasoning_models_get_max_completion_tokens_and_no_temperature(self):
        p = OpenAIProvider(model="gpt-5.4-mini", api_key="k", effort="low")
        p._client = _client_returning(_mock_openai_response())
        await p._call([{"role": "user", "content": "x"}], max_tokens=1000, temperature=0.3)
        payload = p._client.post.call_args[1]["json"]
        assert payload["max_completion_tokens"] == 1000
        assert "max_tokens" not in payload and "temperature" not in payload
        assert payload["reasoning_effort"] == "low"

    @pytest.mark.asyncio
    async def test_json_schema_becomes_strict_structured_output(self):
        from src.core.llm_provider import json_schema_format

        schema = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
        p = OpenAIProvider(model="gpt-4o-mini", api_key="k")
        p._client = _client_returning(_mock_openai_response())
        await p._call([{"role": "user", "content": "x"}], response_format=json_schema_format(schema, "extraction"))
        payload = p._client.post.call_args[1]["json"]
        assert payload["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "extraction", "schema": schema, "strict": True},
        }

    @pytest.mark.asyncio
    async def test_rejected_parameter_is_dropped_and_retried(self):
        rejected = _json_response(
            400,
            {
                "error": {
                    "message": "Unsupported parameter: 'temperature'",
                    "param": "temperature",
                    "code": "unsupported_parameter",
                }
            },
        )
        p = OpenAIProvider(model="my-azure-deployment", api_key="k")
        p._client = _client_returning(rejected, _mock_openai_response())
        result = await p._call([{"role": "user", "content": "x"}])
        assert result.content == '{"balance": 100}'
        second_payload = p._client.post.call_args_list[1][1]["json"]
        assert "temperature" not in second_payload

    @pytest.mark.asyncio
    async def test_max_tokens_is_renamed_when_the_model_wants_max_completion_tokens(self):
        rejected = _json_response(
            400, {"error": {"message": "Use 'max_completion_tokens' instead.", "param": "max_tokens"}}
        )
        p = OpenAIProvider(model="custom-reasoner", api_key="k")
        p._client = _client_returning(rejected, _mock_openai_response())
        await p._call([{"role": "user", "content": "x"}], max_tokens=77)
        second_payload = p._client.post.call_args_list[1][1]["json"]
        assert second_payload["max_completion_tokens"] == 77 and "max_tokens" not in second_payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "choice, message",
        [
            ({"message": {"content": '{"a": 1'}, "finish_reason": "length"}, "ran out of tokens"),
            ({"message": {"content": None, "refusal": "I can't help"}, "finish_reason": "stop"}, "declined"),
            ({"message": {"content": ""}, "finish_reason": "stop"}, "empty"),
        ],
    )
    async def test_unusable_replies_are_provider_errors(self, choice, message):
        p = OpenAIProvider(model="gpt-4o-mini", api_key="k")
        p._client = _client_returning(_json_response(200, {"choices": [choice], "model": "gpt-4o-mini"}))
        with pytest.raises(LLMProviderError, match=message):
            await p._call([{"role": "user", "content": "x"}])

    @pytest.mark.asyncio
    async def test_non_json_body_is_a_provider_error(self):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.side_effect = ValueError("not json")
        p = OpenAIProvider(model="gpt-4o-mini", api_key="k")
        p._client = _client_returning(resp)
        with pytest.raises(LLMProviderError, match="not JSON"):
            await p._call([{"role": "user", "content": "x"}])


_SCHEMA = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}


class TestAnthropicRequestShape:
    @pytest.mark.asyncio
    async def test_images_become_base64_image_blocks(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_reply())
        p = AnthropicProvider(api_key="k")
        await p._call(
            [
                {"role": "system", "content": "sys"},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD", "detail": "high"}},
                    ],
                },
            ]
        )
        body = anthropic_api.body()
        blocks = body["messages"][0]["content"]
        assert blocks[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
        assert blocks[1] == {"type": "text", "text": "read this"}
        assert body["system"] == "sys"

    @pytest.mark.asyncio
    async def test_structured_output_effort_and_fallbacks_on_current_models(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_reply())
        p = AnthropicProvider(model="claude-opus-5", api_key="k", effort="low")
        await p._call([{"role": "user", "content": "x"}], response_format=json_schema_format(_SCHEMA))

        request = anthropic_api.requests[0]
        assert request.url == "https://api.anthropic.com/v1/messages?beta=true"
        assert request.headers["anthropic-beta"] == ANTHROPIC_FALLBACK_BETA
        body = anthropic_api.body()
        assert body["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}}
        assert body["fallbacks"] == "default"
        assert "temperature" not in body

    @pytest.mark.asyncio
    async def test_older_models_keep_temperature_and_skip_unsupported_features(self, anthropic_api):
        anthropic_api.replies.append(_anthropic_reply(model="claude-sonnet-4-5"))
        p = AnthropicProvider(model="claude-sonnet-4-5", api_key="k", effort="low")
        await p._call([{"role": "user", "content": "x"}], response_format=json_schema_format({"type": "object"}))

        request = anthropic_api.requests[0]
        assert request.url == "https://api.anthropic.com/v1/messages"
        assert "anthropic-beta" not in request.headers
        body = anthropic_api.body()
        assert body["temperature"] == 0.0
        assert "output_config" not in body and "fallbacks" not in body

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "base_url, server_side_fallbacks",
        [("https://llm-proxy.internal.example", True), ("https://api.anthropic.com", False)],
        ids=["through-a-proxy", "turned-off"],
    )
    async def test_no_fallbacks(self, anthropic_api, base_url, server_side_fallbacks):
        anthropic_api.replies.append(_anthropic_reply())
        p = AnthropicProvider(
            model="claude-opus-5", api_key="k", base_url=base_url, server_side_fallbacks=server_side_fallbacks
        )
        await p._call([{"role": "user", "content": "x"}])

        request = anthropic_api.requests[0]
        assert request.url == f"{base_url}/v1/messages"
        assert "anthropic-beta" not in request.headers
        assert "fallbacks" not in anthropic_api.body()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "events, message",
        [
            (_message_events(stop_reason="refusal"), "declined"),
            (_message_events(stop_reason="max_tokens"), "ran out of tokens"),
            (_message_events(text=""), "empty reply"),
            # The stream closes before message_delta brings a stop reason.
            (_message_events()[:-2], "ended before the reply was complete"),
        ],
        ids=["refusal", "max-tokens", "empty", "cut-short"],
    )
    async def test_unusable_replies_are_provider_errors(self, anthropic_api, events, message):
        anthropic_api.replies.append(_sse(*events))
        p = AnthropicProvider(api_key="k")
        with pytest.raises(LLMProviderError, match=message):
            await p._call([{"role": "user", "content": "x"}])

    @pytest.mark.asyncio
    async def test_rejected_beta_is_dropped_and_retried(self, anthropic_api):
        anthropic_api.replies += [
            _anthropic_error(
                400,
                "invalid_request_error",
                "Unexpected value(s) `server-side-fallback-2026-07-01` for the `anthropic-beta` header.",
            ),
            _anthropic_reply(),
        ]
        p = AnthropicProvider(model="claude-opus-5", api_key="k")

        result = await p._call([{"role": "user", "content": "x"}])

        assert result.content == '{"ok": true}'
        rejected, retry = anthropic_api.requests
        assert rejected.headers["anthropic-beta"] == ANTHROPIC_FALLBACK_BETA
        assert retry.url == "https://api.anthropic.com/v1/messages"
        assert "anthropic-beta" not in retry.headers
        assert "fallbacks" not in anthropic_api.body(1)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "complaint, retried_output_config",
        [
            (
                "output_config.effort: Extra inputs are not permitted",
                {"format": {"type": "json_schema", "schema": _SCHEMA}},
            ),
            ("output_config.format: structured outputs are not supported for this model", {"effort": "low"}),
        ],
        ids=["effort", "format"],
    )
    async def test_rejected_output_config_is_dropped_and_retried(self, anthropic_api, complaint, retried_output_config):
        anthropic_api.replies += [_anthropic_error(400, "invalid_request_error", complaint), _anthropic_reply()]
        p = AnthropicProvider(model="claude-opus-5", api_key="k", effort="low")

        await p._call([{"role": "user", "content": "x"}], response_format=json_schema_format(_SCHEMA))

        retry = anthropic_api.body(1)
        assert retry["output_config"] == retried_output_config
        assert retry["fallbacks"] == "default"

    @pytest.mark.asyncio
    async def test_rejected_temperature_is_dropped_and_retried(self, anthropic_api):
        anthropic_api.replies += [
            _anthropic_error(400, "invalid_request_error", "temperature: sampling is not supported for this model"),
            _anthropic_reply(),
        ]
        p = AnthropicProvider(model="claude-sonnet-4-5", api_key="k")

        await p._call([{"role": "user", "content": "x"}])

        assert anthropic_api.body(0)["temperature"] == 0.0
        assert "temperature" not in anthropic_api.body(1)

    @pytest.mark.asyncio
    async def test_a_400_that_names_nothing_to_drop_is_not_retried(self, anthropic_api):
        anthropic_api.replies.append(
            _anthropic_error(400, "invalid_request_error", "messages: roles must alternate between user and assistant")
        )
        p = AnthropicProvider(model="claude-opus-5", api_key="k")

        with pytest.raises(LLMProviderError, match="400: messages: roles must alternate"):
            await p._call([{"role": "user", "content": "x"}])
        assert len(anthropic_api.requests) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error, message",
        [
            (httpx2.ReadTimeout("no data within the read timeout"), "timed out after 60.0s"),
            (httpx2.RemoteProtocolError("peer closed connection"), "request failed: RemoteProtocolError"),
        ],
        ids=["read-timeout", "disconnect"],
    )
    async def test_a_stream_that_breaks_is_a_provider_error(self, anthropic_api, error, message):
        anthropic_api.replies.append(_broken_stream(error))
        p = AnthropicProvider(api_key="k")
        with pytest.raises(LLMProviderError, match=message):
            await p._call([{"role": "user", "content": "x"}])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "reply",
        [
            # A proxy that ignores "stream": true and answers with the whole message.
            httpx2.Response(200, json={"type": "message", "content": [{"type": "text", "text": "{}"}]}),
            httpx2.Response(200, headers=_SSE_HEADERS, content=b"event: message_start\ndata: {not json\n\n"),
        ],
        ids=["whole-message", "malformed-event"],
    )
    async def test_a_reply_that_is_not_a_message_stream_is_a_provider_error(self, anthropic_api, reply):
        anthropic_api.replies.append(reply)
        p = AnthropicProvider(api_key="k", base_url="https://llm-proxy.internal.example")
        with pytest.raises(LLMProviderError, match="not a message stream"):
            await p._call([{"role": "user", "content": "x"}])

    def test_capability_predicates(self):
        from src.core.llm_provider import (
            anthropic_accepts_sampling,
            anthropic_effort,
            anthropic_supports_structured_output,
        )

        assert not anthropic_accepts_sampling("claude-opus-5")
        assert not anthropic_accepts_sampling("claude-opus-4-7")
        assert anthropic_accepts_sampling("claude-opus-4-6")
        assert anthropic_accepts_sampling("anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert anthropic_supports_structured_output("claude-sonnet-5")
        assert not anthropic_supports_structured_output("claude-3-haiku-20240307")
        assert anthropic_effort("claude-opus-4-5", "max") == "high"
        assert anthropic_effort("claude-sonnet-4-6", "xhigh") == "high"
        assert anthropic_effort("claude-haiku-4-5", "low") is None


class TestFailureWrapping:
    @pytest.mark.asyncio
    async def test_timeouts_and_network_errors_are_provider_errors(self):
        import httpx

        for error in (httpx.ReadTimeout("slow"), httpx.ConnectError("refused")):
            p = OpenAIProvider(model="gpt-4o-mini", api_key="k")
            client = AsyncMock()
            client.post.side_effect = error
            p._client = client
            with pytest.raises(LLMProviderError):
                await p._call([{"role": "user", "content": "x"}])

    @pytest.mark.asyncio
    async def test_chain_falls_back_after_a_timeout(self):
        import httpx

        primary = OpenAIProvider(model="primary", api_key="k")
        primary._client = AsyncMock()
        primary._client.post.side_effect = httpx.ReadTimeout("slow")
        fallback = OpenAIProvider(model="fallback", api_key="k")
        fallback._client = _client_returning(_mock_openai_response(model="fallback"))
        result = await FallbackChain([primary, fallback]).extract("x")
        assert result.model == "fallback"

    @pytest.mark.asyncio
    async def test_chain_falls_back_when_the_anthropic_stream_breaks(self, anthropic_api):
        anthropic_api.replies.append(_broken_stream(httpx2.ReadError("connection reset by peer")))
        fallback = OpenAIProvider(model="fallback", api_key="k")
        fallback._client = _client_returning(_mock_openai_response(model="fallback"))
        result = await FallbackChain([AnthropicProvider(api_key="k"), fallback]).extract("x")
        assert result.model == "fallback"

    @pytest.mark.asyncio
    async def test_chain_falls_back_after_a_reply_that_is_not_json(self):
        primary = OpenAIProvider(model="primary", api_key="k")
        primary._client = _client_returning(_mock_openai_response(content="Sorry, here is some prose."))
        fallback = OpenAIProvider(model="fallback", api_key="k")
        fallback._client = _client_returning(_mock_openai_response(model="fallback"))
        result = await FallbackChain([primary, fallback]).extract("x")
        assert result.model == "fallback"

    @pytest.mark.asyncio
    async def test_single_provider_non_json_reply_raises_provider_error(self):
        p = OpenAIProvider(model="gpt-4o-mini", api_key="k")
        p._client = _client_returning(_mock_openai_response(content="no json here"))
        with pytest.raises(LLMProviderError, match="not JSON"):
            await p.extract("x")


class TestParsing:
    def test_json_inside_prose_and_fences(self):
        content = 'Here is the extracted data:\n```json\n{"data": {"balance": 12.5}}\n```\nLet me know!'
        r = LLMResponse(content=content, model="m", usage=TokenUsage(), latency_ms=0, provider="t")
        assert r.parse_json() == {"data": {"balance": 12.5}}
        r = LLMResponse(content='Result: {"a": 1} (done)', model="m", usage=TokenUsage(), latency_ms=0, provider="t")
        assert r.parse_json() == {"a": 1}

    @pytest.mark.parametrize(
        "headers, expected",
        [
            ({"retry-after": "30"}, 30.0),
            ({"retry-after-ms": "1500"}, 1.5),
            ({"retry-after": "tomorrow-ish"}, None),
            ({"retry-after": "100000"}, 300.0),
            ({}, None),
        ],
    )
    def test_retry_after(self, headers, expected):
        from src.core.llm_provider import parse_retry_after

        assert parse_retry_after(headers) == expected

    def test_retry_after_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from src.core.llm_provider import parse_retry_after

        when = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=20), usegmt=True)
        assert 10 <= parse_retry_after({"retry-after": when}) <= 21
        past = format_datetime(datetime.now(timezone.utc) - timedelta(seconds=20), usegmt=True)
        assert parse_retry_after({"retry-after": past}) == 0.0
