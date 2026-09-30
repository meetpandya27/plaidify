"""
LLM Extraction Provider — pluggable interface for OpenAI, Anthropic, and local models.

Provides a unified async interface for sending extraction prompts to LLMs and
receiving structured JSON responses. Supports model fallback chains (e.g.
try a cheap model first, fall back to a stronger one) and enforces token budgets.

Every way a call can fail — HTTP errors, timeouts, network errors, a body that
is not JSON, a reply that is not the JSON that was asked for, a refusal or a
truncated answer — surfaces as ``LLMProviderError``, so callers can move on to
the next model, the screenshot path or the fallback selectors.

Requests are shaped per model: parameters a model rejects (sampling settings
on current Claude models, ``max_tokens`` / ``temperature`` on OpenAI reasoning
models) are left out, and if the API still objects to one it is dropped and
the call retried once.

Usage:
    provider = create_provider("openai", api_key="sk-...", model="gpt-5.4-mini")
    result = await provider.extract(prompt, max_tokens=4096)
    # result.content — raw text response
    # result.usage.prompt_tokens — input token usage
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from src.config import get_settings
from src.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpenError, retry_with_backoff
from src.logging_config import get_logger
from src.tracing import tracer

logger = get_logger("llm_provider")

_settings = get_settings()
_llm_breaker = CircuitBreaker(
    "llm",
    failure_threshold=_settings.llm_circuit_failure_threshold,
    reset_timeout=_settings.llm_circuit_reset_seconds,
)

DEFAULT_OPENAI_MODEL = "gpt-5.4-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_ANTHROPIC_FIRST_PARTY_HOSTS = frozenset({"api.anthropic.com"})
_MAX_RETRY_AFTER_SECONDS = 300.0
_MAX_PARAMETER_RETRIES = 3

# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenUsage:
    """Token counts from an LLM response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


_FENCED_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class LLMResponse:
    """Unified response from any LLM provider."""

    content: str
    model: str
    usage: TokenUsage
    latency_ms: float
    provider: str
    raw: Dict[str, Any] = field(default_factory=dict)

    def parse_json(self) -> Any:
        """Extract JSON from the response content.

        Accepts bare JSON, JSON in a markdown code fence, and JSON wrapped in a
        sentence of prose ("Here is the data: {...}").

        Raises:
            json.JSONDecodeError: when no JSON value can be found.
        """
        text = self.content.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as error:
            first_error = error

        fenced = _FENCED_BLOCK.search(text)
        if fenced:
            try:
                return json.loads(fenced.group(1).strip())
            except json.JSONDecodeError:
                pass
        if text.startswith("```"):
            # An opening fence with no closing one.
            try:
                return json.loads(text.split("\n", 1)[1] if "\n" in text else "")
            except json.JSONDecodeError:
                pass

        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = text.find(opener), text.rfind(closer)
            if 0 <= start < end:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        raise first_error


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""


class LLMRateLimitError(LLMProviderError):
    """Raised when the LLM provider returns a rate-limit or overload error (429 / 529)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMAuthError(LLMProviderError):
    """Raised when the LLM provider rejects authentication (401/403)."""


class LLMBudgetExceededError(LLMProviderError):
    """Raised when a request would exceed the token budget."""


def parse_retry_after(headers: Any) -> Optional[float]:
    """Seconds to wait before retrying, from ``retry-after-ms`` / ``retry-after``.

    ``Retry-After`` may be delay-seconds or an HTTP date; anything unparseable
    is ignored rather than raised. The result is clamped to [0, 300].
    """
    if not headers:
        return None

    def _get(name: str) -> Optional[str]:
        try:
            value = headers.get(name)
        except Exception:
            return None
        return value if isinstance(value, str) and value.strip() else None

    delay: Optional[float] = None
    millis = _get("retry-after-ms")
    if millis is not None:
        try:
            delay = float(millis) / 1000
        except ValueError:
            delay = None
    if delay is None:
        value = _get("retry-after")
        if value is not None:
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (TypeError, ValueError, IndexError, OverflowError):
                    delay = None
    if delay is None or delay != delay:  # NaN
        return None
    return max(0.0, min(_MAX_RETRY_AFTER_SECONDS, delay))


def ensure_json_reply(response: LLMResponse) -> None:
    """Raise LLMProviderError unless ``response`` carries parseable JSON."""
    try:
        response.parse_json()
    except (ValueError, TypeError) as exc:
        raise LLMProviderError(f"{response.provider} model {response.model} returned a reply that is not JSON") from exc


def json_object_format() -> Dict[str, Any]:
    return {"type": "json_object"}


def json_schema_format(schema: Dict[str, Any], name: str = "extraction") -> Dict[str, Any]:
    """A provider-neutral structured-output request: this exact JSON schema."""
    return {"type": "json_schema", "name": name, "schema": schema}


def _error_body(resp: Any) -> tuple[str, str]:
    """(param, message) from an API error body, lower-cased; empty when unreadable."""
    try:
        body = resp.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        text = getattr(resp, "text", "")
        return "", text.lower() if isinstance(text, str) else ""
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    param = error.get("param") if isinstance(error.get("param"), str) else ""
    message = error.get("message") if isinstance(error.get("message"), str) else ""
    return param.lower(), message.lower()


def _error_excerpt(resp: Any) -> str:
    text = getattr(resp, "text", "")
    return text[:500] if isinstance(text, str) else ""


# ── Abstract Base ─────────────────────────────────────────────────────────────


class BaseLLMProvider(ABC):
    """Abstract base for all LLM providers."""

    provider_name: str = "base"

    def __init__(
        self,
        model: str,
        *,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        effort: Optional[str] = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.effort = effort

    @abstractmethod
    async def _call(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        """Send messages to the LLM and return a response.

        ``response_format`` is provider-neutral: :func:`json_object_format` or
        :func:`json_schema_format`. Messages may carry OpenAI-style content
        parts (``text`` / ``image_url``); providers translate them.
        """

    async def extract(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = True,
        json_schema: Optional[Dict[str, Any]] = None,
        schema_name: str = "extraction",
    ) -> LLMResponse:
        """Send an extraction prompt and return the response.

        Args:
            prompt: The user prompt containing DOM + field definitions.
            system_prompt: Optional system message for role context.
            max_tokens: Override default max_tokens for this call.
            temperature: Override default temperature for this call.
            json_mode: Request JSON output format from the model.
            json_schema: Request output matching exactly this JSON schema
                (structured outputs) where the model supports it.
            schema_name: Name for the schema, where the API wants one.

        Raises:
            LLMProviderError: on any failure, including a reply that is not JSON
                when JSON was requested.
        """
        messages: List[Dict[str, Any]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response_format: Optional[Dict[str, Any]] = None
        if json_schema is not None:
            response_format = json_schema_format(json_schema, schema_name)
        elif json_mode:
            response_format = json_object_format()

        with tracer.start_as_current_span("llm.extract") as _span:
            _span.set_attribute("llm.provider", self.provider_name)
            _span.set_attribute("llm.model", self.model)
            response = await self._call(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
            )
            _span.set_attribute("llm.prompt_tokens", response.usage.prompt_tokens)
            _span.set_attribute("llm.completion_tokens", response.usage.completion_tokens)
        if response_format is not None:
            ensure_json_reply(response)
        logger.info(
            "LLM extraction complete: provider=%s model=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.1f",
            self.provider_name,
            response.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            response.latency_ms,
        )
        return response

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources (HTTP clients, etc.)."""
        ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def _post(self, client: Any, path: str, payload: Dict[str, Any], **kwargs: Any) -> Any:
        """POST, turning timeouts and network errors into LLMProviderError."""
        import httpx

        try:
            return await client.post(path, json=payload, **kwargs)
        except httpx.TimeoutException as exc:
            raise LLMProviderError(f"{self.provider_name} request timed out after {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"{self.provider_name} request failed: {type(exc).__name__}") from exc


# ── OpenAI Provider ───────────────────────────────────────────────────────────


_OPENAI_REASONING_MODEL = re.compile(r"^(?:o\d|gpt-(?:[5-9]|\d{2,}))")
_OPENAI_EFFORTS = {"low": "low", "medium": "medium", "high": "high", "xhigh": "xhigh", "max": "xhigh"}


def is_openai_reasoning_model(model: str) -> bool:
    """OpenAI reasoning models (o-series, GPT-5 and later) take max_completion_tokens and no temperature."""
    name = (model or "").lower().rsplit("/", 1)[-1]
    return bool(_OPENAI_REASONING_MODEL.match(name)) and "-chat" not in name


class OpenAIProvider(BaseLLMProvider):
    """OpenAI-compatible provider (works with OpenAI, Azure OpenAI, local servers)."""

    provider_name = "openai"

    def __init__(
        self,
        model: str = DEFAULT_OPENAI_MODEL,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        effort: Optional[str] = None,
    ):
        super().__init__(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            effort=effort,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._client: Optional[Any] = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    def _payload(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"model": self.model, "messages": messages}
        limit = max_tokens or self.max_tokens
        if is_openai_reasoning_model(self.model):
            payload["max_completion_tokens"] = limit
            if self.effort in _OPENAI_EFFORTS:
                payload["reasoning_effort"] = _OPENAI_EFFORTS[self.effort]
        else:
            payload["max_tokens"] = limit
            payload["temperature"] = temperature if temperature is not None else self.temperature

        if response_format and response_format.get("type") == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format.get("name", "extraction"),
                    "schema": response_format["schema"],
                    "strict": True,
                },
            }
        elif response_format:
            payload["response_format"] = response_format
        return payload

    @staticmethod
    def _adapt(payload: Dict[str, Any], resp: Any) -> bool:
        """Drop or rename the parameter a 400 complains about; True if the call is worth retrying."""
        param, message = _error_body(resp)

        def mentions(name: str) -> bool:
            return param == name or name in message

        if mentions("max_tokens") and "max_tokens" in payload:
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            return True
        if mentions("temperature") and "temperature" in payload:
            payload.pop("temperature")
            return True
        if mentions("reasoning_effort") and "reasoning_effort" in payload:
            payload.pop("reasoning_effort")
            return True
        if (param.startswith("response_format") or "response_format" in message or "json_schema" in message) and (
            "response_format" in payload
        ):
            if payload["response_format"].get("type") == "json_schema":
                payload["response_format"] = json_object_format()
            else:
                payload.pop("response_format")
            return True
        return False

    async def _call(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        client = self._get_client()
        payload = self._payload(messages, max_tokens, temperature, response_format)

        start = time.monotonic()
        for _attempt in range(_MAX_PARAMETER_RETRIES + 1):
            resp = await self._post(client, "/chat/completions", payload)
            if resp.status_code == 400 and _attempt < _MAX_PARAMETER_RETRIES and self._adapt(payload, resp):
                logger.info("OpenAI rejected a parameter for %s; retrying without it", self.model)
                continue
            break
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 429:
            raise LLMRateLimitError(
                "OpenAI rate limit exceeded",
                retry_after=parse_retry_after(resp.headers),
            )
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"OpenAI authentication failed: {resp.status_code}")
        if resp.status_code != 200:
            raise LLMProviderError(f"OpenAI API error {resp.status_code}: {_error_excerpt(resp)}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMProviderError("OpenAI returned a body that is not JSON") from exc

        try:
            choice = data["choices"][0]
            message = choice.get("message") or {}
            finish_reason = choice.get("finish_reason")
            usage = data.get("usage") or {}
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            raise LLMProviderError("OpenAI returned an unexpected response shape") from exc

        if message.get("refusal"):
            raise LLMProviderError(f"OpenAI model {self.model} declined the request")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if finish_reason == "length":
            raise LLMProviderError(f"OpenAI model {self.model} ran out of tokens before finishing its reply")
        if finish_reason == "content_filter":
            raise LLMProviderError(f"OpenAI model {self.model} reply was withheld by the content filter")
        if not isinstance(content, str) or not content.strip():
            raise LLMProviderError(f"OpenAI model {self.model} returned an empty reply")

        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage=TokenUsage(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                total_tokens=usage.get("total_tokens", 0),
            ),
            latency_ms=latency_ms,
            provider=self.provider_name,
            raw=data,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── Anthropic Provider ────────────────────────────────────────────────────────


def _claude_name(model: str) -> str:
    """The ``claude-…`` part of a model id (drops Bedrock / Vertex style prefixes)."""
    name = (model or "").lower()
    index = name.find("claude-")
    return name[index:] if index >= 0 else name


# Models that still accept temperature / top_p (Claude 3.x through the 4.6 family).
# Opus 4.7 and everything newer reject sampling parameters with a 400.
_ANTHROPIC_SAMPLING = re.compile(r"^claude-(?:3|instant|(?:opus|sonnet|haiku)-4(?:-[0-6])?(?:$|[-@]\d{6,8}|@))")
# Models with structured outputs (output_config.format).
_ANTHROPIC_STRUCTURED = re.compile(r"^claude-(?:fable-5|mythos-5|opus-5|sonnet-5|opus-4-8|haiku-4-5|opus-4-5|opus-4-1)")
# Models that take output_config.effort.
_ANTHROPIC_EFFORT = re.compile(r"^claude-(?:fable|mythos|opus-5|sonnet-5|opus-4-[5-8]|sonnet-4-6)")
# Models whose safety classifiers can decline a request; they get server-side fallbacks.
_ANTHROPIC_REFUSAL_CLASSIFIERS = re.compile(r"^claude-(?:opus-5|fable-5-1|mythos-5-1)")


def anthropic_accepts_sampling(model: str) -> bool:
    return bool(_ANTHROPIC_SAMPLING.match(_claude_name(model)))


def anthropic_supports_structured_output(model: str) -> bool:
    return bool(_ANTHROPIC_STRUCTURED.match(_claude_name(model)))


def anthropic_effort(model: str, effort: Optional[str]) -> Optional[str]:
    """The effort level to send for ``model``, mapped to what that model accepts."""
    name = _claude_name(model)
    if not effort or not _ANTHROPIC_EFFORT.match(name):
        return None
    if name.startswith("claude-opus-4-5"):
        return effort if effort in ("low", "medium", "high") else "high"
    if name.startswith(("claude-opus-4-6", "claude-sonnet-4-6")):
        return "high" if effort == "xhigh" else effort
    return effort


def _anthropic_content(content: Any) -> Any:
    """Translate OpenAI-style content parts into Anthropic content blocks (images first)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return content
    images: List[Dict[str, Any]] = []
    others: List[Dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                header, _, data = url.partition(",")
                media_type = header[len("data:") :].split(";", 1)[0] or "image/png"
                images.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            elif url:
                images.append({"type": "image", "source": {"type": "url", "url": url}})
        elif kind == "image":
            images.append(part)
        elif kind == "text":
            others.append({"type": "text", "text": part.get("text", "")})
        else:
            others.append(part)
    return images + others


def _anthropic_error_message(error: Any) -> str:
    """The API's own message from an SDK status error (the raw body when that is not the usual JSON)."""
    body = error.body
    detail = body.get("error") if isinstance(body, dict) else None
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return detail["message"]
    return body if isinstance(body, str) else error.message


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude provider (Messages API through the official ``anthropic`` SDK)."""

    provider_name = "anthropic"

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
        effort: Optional[str] = None,
        server_side_fallbacks: bool = True,
    ):
        super().__init__(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
            effort=effort,
        )
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.server_side_fallbacks = server_side_fallbacks
        self._client: Optional[Any] = None

    def _get_client(self):
        if self._client is None:
            import anthropic

            # No retries inside the SDK: rate limits and overloads go back to the
            # caller, where FallbackChain backs off or moves on to the next model.
            self._client = anthropic.AsyncAnthropic(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=0,
            )
        return self._client

    def _wants_server_side_fallbacks(self) -> bool:
        host = (urlsplit(self.base_url).hostname or "").lower()
        return (
            self.server_side_fallbacks
            and host in _ANTHROPIC_FIRST_PARTY_HOSTS
            and bool(_ANTHROPIC_REFUSAL_CLASSIFIERS.match(_claude_name(self.model)))
        )

    def _params(
        self,
        messages: List[Dict[str, Any]],
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Keyword arguments for ``messages.stream`` (``beta.messages.stream`` when they carry ``fallbacks``)."""
        # Anthropic uses a separate 'system' parameter
        system_text = None
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                user_messages.append({**msg, "content": _anthropic_content(msg.get("content"))})

        params: Dict[str, Any] = {
            "model": self.model,
            "messages": user_messages,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if system_text:
            params["system"] = system_text
        if anthropic_accepts_sampling(self.model):
            # The SDK no longer takes sampling arguments; the models that still
            # accept temperature read it from the request body.
            params["extra_body"] = {"temperature": temperature if temperature is not None else self.temperature}

        output_config: Dict[str, Any] = {}
        effort = anthropic_effort(self.model, self.effort)
        if effort:
            output_config["effort"] = effort
        if (
            response_format
            and response_format.get("type") == "json_schema"
            and anthropic_supports_structured_output(self.model)
        ):
            output_config["format"] = {"type": "json_schema", "schema": response_format["schema"]}
        if output_config:
            params["output_config"] = output_config

        if self._wants_server_side_fallbacks():
            params["betas"] = [ANTHROPIC_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    @staticmethod
    def _adapt(params: Dict[str, Any], message: str) -> bool:
        """Drop the parameter a 400's error ``message`` complains about; True if the call is worth retrying."""
        message = message.lower()
        output_config = params.get("output_config") or {}
        sampling = params.get("extra_body") or {}

        if ("fallback" in message or "anthropic-beta" in message) and "fallbacks" in params:
            params.pop("fallbacks")
            params.pop("betas", None)
            return True
        if ("temperature" in message or "top_p" in message or "sampling" in message) and "temperature" in sampling:
            params.pop("extra_body")
            return True
        if "effort" in message and "effort" in output_config:
            output_config.pop("effort")
            if not output_config:
                params.pop("output_config", None)
            return True
        if ("output_config" in message or "format" in message or "schema" in message) and "format" in output_config:
            output_config.pop("format")
            if not output_config:
                params.pop("output_config", None)
            return True
        return False

    def _status_error(self, error: Any) -> LLMProviderError:
        """The LLMProviderError for an error status from the API, or an error event in its stream."""
        status = error.status_code
        # An overload during the stream arrives as an error event on a 200 response.
        if status in (429, 529) or error.type == "overloaded_error":
            return LLMRateLimitError(
                "Anthropic rate limit exceeded" if status == 429 else "Anthropic API overloaded",
                retry_after=parse_retry_after(error.response.headers),
            )
        if status in (401, 403):
            return LLMAuthError(f"Anthropic authentication failed: {status}")
        return LLMProviderError(f"Anthropic API error {status}: {_anthropic_error_message(error)[:500]}")

    async def _stream(self, client: Any, params: Dict[str, Any]) -> Any:
        """Send one request and read its event stream through to the final message."""
        # Streamed, the read timeout bounds the gap between events rather than
        # the whole reply, so a long answer or a long think can finish.
        messages_api = client.beta.messages if "fallbacks" in params else client.messages
        async with messages_api.stream(**params) as stream:
            return await stream.get_final_message()

    async def _call(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        import anthropic
        import httpx2

        client = self._get_client()
        params = self._params(messages, max_tokens, temperature, response_format)

        start = time.monotonic()
        for _attempt in range(_MAX_PARAMETER_RETRIES + 1):
            try:
                message = await self._stream(client, params)
            except anthropic.BadRequestError as error:
                if _attempt < _MAX_PARAMETER_RETRIES and self._adapt(params, _anthropic_error_message(error)):
                    logger.info("Anthropic rejected a parameter for %s; retrying without it", self.model)
                    continue
                raise self._status_error(error) from error
            except anthropic.APIStatusError as error:
                raise self._status_error(error) from error
            # The SDK wraps a failure to send the request; one that breaks the
            # stream later arrives as the raw httpx2 error.
            except (anthropic.APITimeoutError, httpx2.TimeoutException) as error:
                raise LLMProviderError(f"{self.provider_name} request timed out after {self.timeout}s") from error
            except (anthropic.APIConnectionError, httpx2.HTTPError) as error:
                failure = type(error.__cause__ or error).__name__
                raise LLMProviderError(f"{self.provider_name} request failed: {failure}") from error
            except (AssertionError, LookupError, RuntimeError, ValueError) as error:
                # get_final_message() over a body that is not an event stream (a
                # proxy that ignored "stream": true) or over a malformed event.
                raise LLMProviderError("Anthropic returned a reply that is not a message stream") from error
            break
        latency_ms = (time.monotonic() - start) * 1000

        if message.stop_reason == "refusal":
            raise LLMProviderError(f"Anthropic model {message.model or self.model} declined the request")
        if message.stop_reason == "max_tokens":
            raise LLMProviderError(f"Anthropic model {self.model} ran out of tokens before finishing its reply")
        if message.stop_reason is None:
            # A finished message always has a stop reason; the stream closed early.
            raise LLMProviderError(f"Anthropic model {self.model} stream ended before the reply was complete")

        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise LLMProviderError(f"Anthropic model {self.model} returned an empty reply")
        usage = message.usage

        return LLMResponse(
            content=text,
            model=message.model or self.model,
            usage=TokenUsage(
                prompt_tokens=usage.input_tokens,
                completion_tokens=usage.output_tokens,
                total_tokens=usage.input_tokens + usage.output_tokens,
            ),
            latency_ms=latency_ms,
            provider=self.provider_name,
            raw=message.to_dict(),
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None


# ── Fallback Chain ────────────────────────────────────────────────────────────


class FallbackChain(BaseLLMProvider):
    """Tries providers in order, falling back on failure.

    Useful for cost optimization: try a cheap model first, fall back to a
    more capable one if extraction fails or returns low-confidence results.
    A reply that is not the JSON that was asked for counts as a failure.

    Usage:
        chain = FallbackChain([
            OpenAIProvider(model="gpt-5.4-mini", api_key=key),
            OpenAIProvider(model="gpt-5.4", api_key=key),
        ])
        result = await chain.extract(prompt)
    """

    provider_name = "fallback_chain"

    def __init__(self, providers: Sequence[BaseLLMProvider]):
        if not providers:
            raise ValueError("FallbackChain requires at least one provider")
        # Use first provider's defaults
        first = providers[0]
        super().__init__(
            model=first.model,
            max_tokens=first.max_tokens,
            temperature=first.temperature,
            timeout=first.timeout,
            effort=getattr(first, "effort", None),
        )
        self.providers = list(providers)

    async def _call(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> LLMResponse:
        async def _run_chain() -> LLMResponse:
            return await self._call_chain(
                messages,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
            )

        # Fail fast when the whole LLM subsystem is degraded instead of paying
        # the full timeout on every request.
        try:
            return await _llm_breaker.call(_run_chain)
        except CircuitBreakerOpenError as e:
            raise LLMProviderError(str(e)) from e

    async def _call_chain(
        self,
        messages: List[Dict[str, Any]],
        *,
        max_tokens: Optional[int],
        temperature: Optional[float],
        response_format: Optional[Dict[str, Any]],
    ) -> LLMResponse:
        last_error: Optional[Exception] = None
        for provider in self.providers:

            async def _attempt(provider: BaseLLMProvider = provider) -> LLMResponse:
                response = await provider._call(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_format=response_format,
                )
                if response_format is not None:
                    ensure_json_reply(response)
                return response

            try:
                # Retry only genuinely transient (rate-limit) errors, honouring
                # any retry-after hint, with bounded exponential backoff.
                return await retry_with_backoff(
                    _attempt,
                    retries=_settings.llm_retry_max_attempts,
                    retry_on=(LLMRateLimitError,),
                    delay_for=lambda e: getattr(e, "retry_after", None),
                )
            except LLMAuthError:
                raise  # Don't retry auth errors
            except LLMProviderError as e:
                logger.warning(
                    "Provider failed, trying next: provider=%s model=%s error=%s",
                    provider.provider_name,
                    provider.model,
                    str(e),
                )
                last_error = e
                continue
        raise LLMProviderError(f"All providers in fallback chain failed. Last error: {last_error}")

    async def close(self) -> None:
        for provider in self.providers:
            await provider.close()


# ── Factory ───────────────────────────────────────────────────────────────────


def create_provider(
    provider_type: str,
    *,
    api_key: str,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    timeout: float = 60.0,
    effort: Optional[str] = None,
    server_side_fallbacks: Optional[bool] = None,
) -> BaseLLMProvider:
    """Create an LLM provider by name.

    Args:
        provider_type: One of 'openai', 'anthropic'.
        api_key: API key for the provider.
        model: Model name (uses provider default if not specified).
        base_url: Override the API base URL (useful for Azure OpenAI / local servers).
        max_tokens: Max tokens for completions.
        temperature: Sampling temperature (0.0 = deterministic), for models that take one.
        timeout: HTTP timeout in seconds.
        effort: Reasoning effort for models that take one ('low' … 'max').
        server_side_fallbacks: Anthropic only; defaults to LLM_SERVER_SIDE_FALLBACKS.

    Returns:
        A configured LLM provider instance.
    """
    provider_type = provider_type.lower().strip()

    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
        "effort": effort,
    }
    if base_url:
        kwargs["base_url"] = base_url

    if provider_type == "openai":
        return OpenAIProvider(model=model or DEFAULT_OPENAI_MODEL, **kwargs)
    elif provider_type == "anthropic":
        if server_side_fallbacks is None:
            server_side_fallbacks = _settings.llm_server_side_fallbacks
        return AnthropicProvider(
            model=model or DEFAULT_ANTHROPIC_MODEL,
            server_side_fallbacks=server_side_fallbacks,
            **kwargs,
        )
    else:
        raise ValueError(f"Unknown provider type: {provider_type!r}. Supported: 'openai', 'anthropic'")
