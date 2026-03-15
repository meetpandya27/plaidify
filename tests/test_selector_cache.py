"""Tests for selector caching layer."""

import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from src.core.selector_cache import (
    CacheEntry,
    SelectorCache,
    make_cache_key,
    DEFAULT_TTL,
    MAX_FAILURES,
)


# ── Cache Key ─────────────────────────────────────────────────────────────────


class TestMakeCacheKey:
    def test_deterministic(self):
        k1 = make_cache_key("example.com", "/dashboard")
        k2 = make_cache_key("example.com", "/dashboard")
        assert k1 == k2

    def test_different_domains(self):
        k1 = make_cache_key("site-a.com", "/page")
        k2 = make_cache_key("site-b.com", "/page")
        assert k1 != k2

    def test_different_paths(self):
        k1 = make_cache_key("example.com", "/page1")
        k2 = make_cache_key("example.com", "/page2")
        assert k1 != k2

    def test_case_insensitive_domain(self):
        k1 = make_cache_key("Example.COM", "/page")
        k2 = make_cache_key("example.com", "/page")
        assert k1 == k2

    def test_strips_leading_trailing_slash(self):
        k1 = make_cache_key("example.com", "/dashboard/")
        k2 = make_cache_key("example.com", "dashboard")
        assert k1 == k2

    def test_returns_hex_string(self):
        key = make_cache_key("example.com", "/page")
        assert len(key) == 16
        assert all(c in "0123456789abcdef" for c in key)


# ── CacheEntry ────────────────────────────────────────────────────────────────


class TestCacheEntry:
    def _make_entry(self, **kwargs) -> CacheEntry:
        defaults = {
            "domain": "example.com",
            "page_path": "/dashboard",
            "selectors": {"balance": "span.bal"},
            "confidence": 0.95,
            "created_at": time.time(),
        }
        defaults.update(kwargs)
        return CacheEntry(**defaults)

    def test_defaults(self):
        e = self._make_entry()
        assert e.ttl == DEFAULT_TTL
        assert e.failure_count == 0
        assert e.hit_count == 0
        assert e.last_failure_at is None
        assert e.last_success_at is None

    def test_is_expired_false(self):
        e = self._make_entry(created_at=time.time())
        assert not e.is_expired

    def test_is_expired_true(self):
        e = self._make_entry(created_at=time.time() - DEFAULT_TTL - 1)
        assert e.is_expired

    def test_is_invalidated_false(self):
        e = self._make_entry(failure_count=MAX_FAILURES - 1)
        assert not e.is_invalidated

    def test_is_invalidated_true(self):
        e = self._make_entry(failure_count=MAX_FAILURES)
        assert e.is_invalidated

    def test_is_usable(self):
        e = self._make_entry()
        assert e.is_usable

    def test_not_usable_expired(self):
        e = self._make_entry(created_at=time.time() - DEFAULT_TTL - 1)
        assert not e.is_usable

    def test_not_usable_invalidated(self):
        e = self._make_entry(failure_count=MAX_FAILURES)
        assert not e.is_usable

    def test_record_success(self):
        e = self._make_entry(failure_count=2)
        e.record_success()
        assert e.failure_count == 0
        assert e.hit_count == 1
        assert e.last_success_at is not None

    def test_record_failure(self):
        e = self._make_entry()
        e.record_failure()
        assert e.failure_count == 1
        assert e.last_failure_at is not None

    def test_record_multiple_failures_invalidates(self):
        e = self._make_entry()
        for _ in range(MAX_FAILURES):
            e.record_failure()
        assert e.is_invalidated

    def test_to_dict(self):
        e = self._make_entry(confidence=0.9, hit_count=5)
        d = e.to_dict()
        assert d["domain"] == "example.com"
        assert d["confidence"] == 0.9
        assert d["selectors"] == {"balance": "span.bal"}
        assert d["hit_count"] == 5

    def test_from_dict_roundtrip(self):
        e = self._make_entry(hit_count=3, failure_count=1)
        d = e.to_dict()
        e2 = CacheEntry.from_dict(d)
        assert e2.domain == e.domain
        assert e2.page_path == e.page_path
        assert e2.selectors == e.selectors
        assert e2.confidence == e.confidence
        assert e2.hit_count == e.hit_count
        assert e2.failure_count == e.failure_count

    def test_from_dict_defaults(self):
        d = {
            "domain": "example.com",
            "page_path": "/page",
            "selectors": {},
            "created_at": time.time(),
        }
        e = CacheEntry.from_dict(d)
        assert e.confidence == 0.0
        assert e.ttl == DEFAULT_TTL
        assert e.failure_count == 0


# ── SelectorCache ─────────────────────────────────────────────────────────────


class TestSelectorCache:
    def test_empty_cache(self):
        cache = SelectorCache()
        assert cache.size == 0
        assert cache.get("example.com", "/page") is None

    def test_put_and_get(self):
        cache = SelectorCache()
        cache.put("example.com", "/dashboard", {"balance": "span.bal"}, confidence=0.9)
        entry = cache.get("example.com", "/dashboard")
        assert entry is not None
        assert entry.selectors == {"balance": "span.bal"}
        assert entry.confidence == 0.9

    def test_get_different_page(self):
        cache = SelectorCache()
        cache.put("example.com", "/dashboard", {"a": "b"})
        assert cache.get("example.com", "/settings") is None

    def test_get_expired_returns_none(self):
        cache = SelectorCache()
        entry = cache.put("example.com", "/page", {"a": "b"}, ttl=1)
        # Force created_at into the past
        entry.created_at = time.time() - 10
        assert cache.get("example.com", "/page") is None

    def test_get_invalidated_returns_none(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"a": "b"})
        for _ in range(MAX_FAILURES):
            cache.record_failure("example.com", "/page")
        assert cache.get("example.com", "/page") is None

    def test_record_success_resets_failures(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"a": "b"})
        cache.record_failure("example.com", "/page")
        cache.record_failure("example.com", "/page")
        cache.record_success("example.com", "/page")

        entry = cache.get("example.com", "/page")
        assert entry is not None
        assert entry.failure_count == 0
        assert entry.hit_count == 1

    def test_record_failure_nonexistent(self):
        cache = SelectorCache()
        cache.record_failure("ghost.com", "/page")  # Should not raise

    def test_record_success_nonexistent(self):
        cache = SelectorCache()
        cache.record_success("ghost.com", "/page")  # Should not raise

    def test_invalidate(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"a": "b"})
        assert cache.invalidate("example.com", "/page") is True
        assert cache.size == 0

    def test_invalidate_nonexistent(self):
        cache = SelectorCache()
        assert cache.invalidate("ghost.com", "/page") is False

    def test_invalidate_domain(self):
        cache = SelectorCache()
        cache.put("example.com", "/page1", {"a": "b"})
        cache.put("example.com", "/page2", {"c": "d"})
        cache.put("other.com", "/page1", {"e": "f"})

        removed = cache.invalidate_domain("example.com")
        assert removed == 2
        assert cache.size == 1
        assert cache.get("other.com", "/page1") is not None

    def test_invalidate_domain_none(self):
        cache = SelectorCache()
        assert cache.invalidate_domain("ghost.com") == 0

    def test_clear(self):
        cache = SelectorCache()
        cache.put("a.com", "/1", {"x": "y"})
        cache.put("b.com", "/2", {"x": "y"})
        cache.clear()
        assert cache.size == 0

    def test_stats(self):
        cache = SelectorCache()
        cache.put("a.com", "/1", {"x": "y"})
        cache.put("b.com", "/2", {"x": "y"})
        cache.record_success("a.com", "/1")
        cache.record_success("a.com", "/1")

        stats = cache.stats()
        assert stats["total_entries"] == 2
        assert stats["usable"] == 2
        assert stats["expired"] == 0
        assert stats["invalidated"] == 0
        assert stats["total_hits"] == 2

    def test_stats_with_expired(self):
        cache = SelectorCache()
        entry = cache.put("a.com", "/1", {"x": "y"}, ttl=1)
        entry.created_at = time.time() - 10
        stats = cache.stats()
        assert stats["expired"] == 1
        assert stats["usable"] == 0

    def test_custom_ttl(self):
        cache = SelectorCache(ttl=3600)
        entry = cache.put("a.com", "/page", {"x": "y"})
        assert entry.ttl == 3600

    def test_put_overrides_existing(self):
        cache = SelectorCache()
        cache.put("a.com", "/page", {"old": "selectors"}, confidence=0.5)
        cache.put("a.com", "/page", {"new": "selectors"}, confidence=0.9)

        entry = cache.get("a.com", "/page")
        assert entry.selectors == {"new": "selectors"}
        assert entry.confidence == 0.9
        assert cache.size == 1


# ── File Persistence ──────────────────────────────────────────────────────────


class TestFilePersistence:
    def test_save_and_load(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")

        # Create and populate cache
        cache1 = SelectorCache(persist_path=cache_file)
        cache1.put("example.com", "/page", {"balance": "span.bal"}, confidence=0.9)
        cache1.record_success("example.com", "/page")

        # Load in new instance
        cache2 = SelectorCache(persist_path=cache_file)
        entry = cache2.get("example.com", "/page")
        assert entry is not None
        assert entry.selectors == {"balance": "span.bal"}
        assert entry.hit_count == 1

    def test_load_missing_file(self, tmp_path):
        cache = SelectorCache(persist_path=str(tmp_path / "nonexistent.json"))
        assert cache.size == 0

    def test_load_corrupt_file(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text("not valid json{{{")

        cache = SelectorCache(persist_path=str(cache_file))
        assert cache.size == 0  # Gracefully handles corrupt data

    def test_persist_on_put(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache = SelectorCache(persist_path=str(cache_file))
        cache.put("a.com", "/page", {"x": "y"})
        assert cache_file.exists()

        data = json.loads(cache_file.read_text())
        assert len(data) == 1

    def test_persist_on_invalidate(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        cache = SelectorCache(persist_path=cache_file)
        cache.put("a.com", "/page", {"x": "y"})
        cache.invalidate("a.com", "/page")

        data = json.loads(Path(cache_file).read_text())
        assert len(data) == 0

    def test_persist_on_clear(self, tmp_path):
        cache_file = str(tmp_path / "cache.json")
        cache = SelectorCache(persist_path=cache_file)
        cache.put("a.com", "/page", {"x": "y"})
        cache.clear()

        data = json.loads(Path(cache_file).read_text())
        assert len(data) == 0

    def test_persist_creates_parent_dirs(self, tmp_path):
        cache_file = str(tmp_path / "nested" / "dir" / "cache.json")
        cache = SelectorCache(persist_path=cache_file)
        cache.put("a.com", "/page", {"x": "y"})
        assert Path(cache_file).exists()


# ── Self-Healing ──────────────────────────────────────────────────────────────


class TestSelfHealing:
    """Test the self-healing behavior: cached selectors invalidate after N failures."""

    def test_gradual_failure(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"bal": "span.bal"})

        # First failure: still usable
        cache.record_failure("example.com", "/page")
        assert cache.get("example.com", "/page") is not None

        # Second failure: still usable
        cache.record_failure("example.com", "/page")
        assert cache.get("example.com", "/page") is not None

        # Third failure: invalidated
        cache.record_failure("example.com", "/page")
        assert cache.get("example.com", "/page") is None

    def test_success_resets_counter(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"bal": "span.bal"})

        cache.record_failure("example.com", "/page")
        cache.record_failure("example.com", "/page")
        # Almost at threshold — success resets
        cache.record_success("example.com", "/page")
        cache.record_failure("example.com", "/page")
        cache.record_failure("example.com", "/page")
        # Still usable because success reset the counter
        assert cache.get("example.com", "/page") is not None

    def test_re_cache_after_invalidation(self):
        cache = SelectorCache()
        cache.put("example.com", "/page", {"old": "selector"})

        for _ in range(MAX_FAILURES):
            cache.record_failure("example.com", "/page")
        assert cache.get("example.com", "/page") is None

        # Re-cache with new selectors from LLM
        cache.put("example.com", "/page", {"new": "selector"}, confidence=0.85)
        entry = cache.get("example.com", "/page")
        assert entry is not None
        assert entry.selectors == {"new": "selector"}
        assert entry.failure_count == 0
