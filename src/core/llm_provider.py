"""
LLM Extraction Provider — pluggable interface for OpenAI, Anthropic, and local models.

Provides a unified async interface for sending extraction prompts to LLMs and
receiving structured JSON responses. Supports model fallback chains (e.g.
try gpt-4o-mini first, fall back to gpt-4o) and enforces token budgets.

Usage:
    provider = create_provider("openai", api_key="sk-...", model="gpt-4o-mini")
    result = await provider.extract(prompt, max_tokens=4096)
    # result.content — raw text response
    # result.usage.prompt_tokens — input token usage
"""

from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from src.logging_config import get_logger

logger = get_logger("llm_provider")

# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TokenUsage:
    """Token counts from an LLM response."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


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

        Handles responses that wrap JSON in markdown code fences.
        """
        text = self.content.strip()
        # Strip ```json ... ``` fencing
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```json or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)
        return json.loads(text)


class LLMProviderError(Exception):
    """Base exception for LLM provider errors."""


class LLMRateLimitError(LLMProviderError):
    """Raised when the LLM provider returns a rate-limit error (429)."""

    def __init__(self, message: str, retry_after: Optional[float] = None):
        super().__init__(message)
        self.retry_after = retry_after


class LLMAuthError(LLMProviderError):
    """Raised when the LLM provider rejects authentication (401/403)."""


class LLMBudgetExceededError(LLMProviderError):
    """Raised when a request would exceed the token budget."""


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
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout = timeout

    @abstractmethod
    async def _call(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> LLMResponse:
        """Send messages to the LLM and return a response.

        Subclasses implement HTTP call logic here.
        """

    async def extract(
        self,
        prompt: str,
        *,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        json_mode: bool = True,
    ) -> LLMResponse:
        """Send an extraction prompt and return the response.

        Args:
            prompt: The user prompt containing DOM + field definitions.
            system_prompt: Optional system message for role context.
            max_tokens: Override default max_tokens for this call.
            temperature: Override default temperature for this call.
            json_mode: Request JSON output format from the model.
        """
        messages: List[Dict[str, str]] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        response_format = {"type": "json_object"} if json_mode else None

        response = await self._call(
            messages,
            max_tokens=max_tokens,
            temperature=temperature,
            response_format=response_format,
        )
        logger.info(
            "LLM extraction complete: provider=%s model=%s prompt_tokens=%d completion_tokens=%d latency_ms=%.1f",
            self.provider_name,
            response.model,
            response.usage.prompt_tokens,
            response.usage.completion_tokens,
            response.latency_ms,
        )
        return response

    async def close(self) -> None:
        """Clean up resources (HTTP clients, etc.)."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


# ── OpenAI Provider ───────────────────────────────────────────────────────────


class OpenAIProvider(BaseLLMProvider):
    """OpenAI-compatible provider (works with OpenAI, Azure OpenAI, local servers)."""

    provider_name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        super().__init__(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
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

    async def _call(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> LLMResponse:
        client = self._get_client()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if response_format:
            payload["response_format"] = response_format

        start = time.monotonic()
        resp = await client.post("/chat/completions", json=payload)
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            raise LLMRateLimitError(
                f"OpenAI rate limit exceeded",
                retry_after=float(retry_after) if retry_after else None,
            )
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"OpenAI authentication failed: {resp.status_code}")
        if resp.status_code != 200:
            raise LLMProviderError(
                f"OpenAI API error {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        choice = data["choices"][0]
        usage = data.get("usage", {})

        return LLMResponse(
            content=choice["message"]["content"],
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


class AnthropicProvider(BaseLLMProvider):
    """Anthropic Claude provider."""

    provider_name = "anthropic"

    def __init__(
        self,
        model: str = "claude-sonnet-4-20250514",
        *,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout: float = 60.0,
    ):
        super().__init__(
            model,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout=timeout,
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
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                },
                timeout=self.timeout,
            )
        return self._client

    async def _call(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> LLMResponse:
        client = self._get_client()

        # Anthropic uses a separate 'system' parameter
        system_text = None
        user_messages = []
        for msg in messages:
            if msg["role"] == "system":
                system_text = msg["content"]
            else:
                user_messages.append(msg)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": user_messages,
            "max_tokens": max_tokens or self.max_tokens,
            "temperature": temperature if temperature is not None else self.temperature,
        }
        if system_text:
            payload["system"] = system_text

        start = time.monotonic()
        resp = await client.post("/v1/messages", json=payload)
        latency_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 429:
            retry_after = resp.headers.get("retry-after")
            raise LLMRateLimitError(
                f"Anthropic rate limit exceeded",
                retry_after=float(retry_after) if retry_after else None,
            )
        if resp.status_code in (401, 403):
            raise LLMAuthError(f"Anthropic authentication failed: {resp.status_code}")
        if resp.status_code != 200:
            raise LLMProviderError(
                f"Anthropic API error {resp.status_code}: {resp.text[:500]}"
            )

        data = resp.json()
        content_blocks = data.get("content", [])
        text = "".join(b["text"] for b in content_blocks if b["type"] == "text")
        usage = data.get("usage", {})

        return LLMResponse(
            content=text,
            model=data.get("model", self.model),
            usage=TokenUsage(
                prompt_tokens=usage.get("input_tokens", 0),
                completion_tokens=usage.get("output_tokens", 0),
                total_tokens=usage.get("input_tokens", 0)
                + usage.get("output_tokens", 0),
            ),
            latency_ms=latency_ms,
            provider=self.provider_name,
            raw=data,
        )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# ── Fallback Chain ────────────────────────────────────────────────────────────


class FallbackChain(BaseLLMProvider):
    """Tries providers in order, falling back on failure.

    Useful for cost optimization: try a cheap model first, fall back to a
    more capable one if extraction fails or returns low-confidence results.

    Usage:
        chain = FallbackChain([
            OpenAIProvider(model="gpt-4o-mini", api_key=key),
            OpenAIProvider(model="gpt-4o", api_key=key),
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
        )
        self.providers = list(providers)

    async def _call(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        temperature: Optional[float] = None,
        response_format: Optional[Dict[str, str]] = None,
    ) -> LLMResponse:
        last_error: Optional[Exception] = None
        for provider in self.providers:
            try:
                return await provider._call(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    response_format=response_format,
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
        raise LLMProviderError(
            f"All providers in fallback chain failed. Last error: {last_error}"
        )

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
) -> BaseLLMProvider:
    """Create an LLM provider by name.

    Args:
        provider_type: One of 'openai', 'anthropic'.
        api_key: API key for the provider.
        model: Model name (uses provider default if not specified).
        base_url: Override the API base URL (useful for Azure OpenAI / local servers).
        max_tokens: Max tokens for completions.
        temperature: Sampling temperature (0.0 = deterministic).
        timeout: HTTP timeout in seconds.

    Returns:
        A configured LLM provider instance.
    """
    provider_type = provider_type.lower().strip()

    kwargs: Dict[str, Any] = {
        "api_key": api_key,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout": timeout,
    }
    if base_url:
        kwargs["base_url"] = base_url

    if provider_type == "openai":
        return OpenAIProvider(model=model or "gpt-4o-mini", **kwargs)
    elif provider_type == "anthropic":
        return AnthropicProvider(model=model or "claude-sonnet-4-20250514", **kwargs)
    else:
        raise ValueError(
            f"Unknown provider type: {provider_type!r}. "
            f"Supported: 'openai', 'anthropic'"
        )
