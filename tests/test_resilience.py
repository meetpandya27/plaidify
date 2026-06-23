"""Tests for resilience primitives: circuit breaker, backoff retry, and the
LLM fallback chain's fail-fast behavior."""

import pytest

from src.core.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerOpenError,
    CircuitState,
    retry_with_backoff,
)


async def _noop_sleep(*_a, **_k):
    return None


# ── CircuitBreaker ───────────────────────────────────────────────────────────


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_opens_after_threshold_then_fails_fast(self):
        cb = CircuitBreaker("t", failure_threshold=3, reset_timeout=60)

        async def boom():
            raise ValueError("x")

        for _ in range(3):
            with pytest.raises(ValueError):
                await cb.call(boom)
        assert cb.state is CircuitState.OPEN

        # Subsequent calls are rejected without invoking the function.
        with pytest.raises(CircuitBreakerOpenError):
            await cb.call(boom)

    @pytest.mark.asyncio
    async def test_half_open_recovers_on_success(self):
        cb = CircuitBreaker("t", failure_threshold=1, reset_timeout=0.0)

        async def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            await cb.call(boom)
        assert cb.state is CircuitState.OPEN

        # reset_timeout=0 → next call is a half-open trial; success closes it.
        async def ok():
            return "ok"

        assert await cb.call(ok) == "ok"
        assert cb.state is CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        cb = CircuitBreaker("t", failure_threshold=3, reset_timeout=60)

        async def boom():
            raise ValueError()

        async def ok():
            return 1

        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(boom)
        await cb.call(ok)  # resets the counter
        # Two more failures must not open the breaker (counter was reset).
        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(boom)
        assert cb.state is CircuitState.CLOSED


# ── retry_with_backoff ───────────────────────────────────────────────────────


class TestRetryWithBackoff:
    @pytest.mark.asyncio
    async def test_succeeds_after_transient_failures(self, monkeypatch):
        monkeypatch.setattr("src.core.circuit_breaker.asyncio.sleep", _noop_sleep)
        calls = {"n": 0}

        async def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise ValueError("transient")
            return "ok"

        assert await retry_with_backoff(flaky, retries=5, retry_on=(ValueError,)) == "ok"
        assert calls["n"] == 3

    @pytest.mark.asyncio
    async def test_reraises_after_exhaustion(self, monkeypatch):
        monkeypatch.setattr("src.core.circuit_breaker.asyncio.sleep", _noop_sleep)

        async def always_fail():
            raise ValueError("nope")

        with pytest.raises(ValueError):
            await retry_with_backoff(always_fail, retries=2, retry_on=(ValueError,))

    @pytest.mark.asyncio
    async def test_does_not_retry_unmatched_exceptions(self, monkeypatch):
        monkeypatch.setattr("src.core.circuit_breaker.asyncio.sleep", _noop_sleep)
        calls = {"n": 0}

        async def fail():
            calls["n"] += 1
            raise KeyError("different")

        with pytest.raises(KeyError):
            await retry_with_backoff(fail, retries=3, retry_on=(ValueError,))
        assert calls["n"] == 1  # not retried

    @pytest.mark.asyncio
    async def test_delay_for_is_consulted(self, monkeypatch):
        seen = []

        async def fake_sleep(d):
            seen.append(d)

        monkeypatch.setattr("src.core.circuit_breaker.asyncio.sleep", fake_sleep)

        async def fail():
            raise ValueError()

        with pytest.raises(ValueError):
            await retry_with_backoff(fail, retries=1, retry_on=(ValueError,), jitter=False, delay_for=lambda _e: 2.0)
        assert seen == [2.0]


# ── LLM fallback chain fail-fast ─────────────────────────────────────────────


class TestLLMChainResilience:
    @pytest.mark.asyncio
    async def test_chain_breaker_opens_and_fails_fast(self):
        from src.core import llm_provider as lp
        from src.core.llm_provider import FallbackChain, LLMProviderError

        lp._llm_breaker.reset()
        try:

            class _Failing:
                provider_name = "failing"
                model = "x"
                max_tokens = 100
                temperature = 0.0
                timeout = 1.0

                async def _call(self, *_a, **_k):
                    raise LLMProviderError("provider down")

                async def close(self):
                    return None

            chain = FallbackChain([_Failing()])
            msgs = [{"role": "user", "content": "x"}]

            for _ in range(lp._settings.llm_circuit_failure_threshold):
                with pytest.raises(LLMProviderError):
                    await chain._call(msgs)

            # Breaker now open — the next call fails fast with a circuit message.
            with pytest.raises(LLMProviderError) as exc_info:
                await chain._call(msgs)
            assert "open" in str(exc_info.value).lower()
        finally:
            lp._llm_breaker.reset()
