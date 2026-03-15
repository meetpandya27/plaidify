"""
Selector Cache — stores LLM-extracted selectors per site+page with TTL and failure tracking.

When the LLM extracts data and returns CSS selectors, we cache them so
subsequent requests can skip the LLM entirely and use deterministic CSS extraction.
If cached selectors fail N times, they're invalidated and the LLM re-runs.

Cache key: (domain, page_hash) where page_hash is derived from the URL path
and structural DOM signature (not content, since balances change).

Usage:
    cache = SelectorCache()
    entry = cache.get("hydroone.com", "/dashboard")
    if entry and not entry.is_expired:
        # Use cached selectors
    else:
        # Run LLM extraction, then cache
        cache.put("hydroone.com", "/dashboard", selectors, confidence=0.95)
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_config import get_logger

logger = get_logger("selector_cache")

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_TTL = 86400 * 7  # 7 days
MAX_FAILURES = 3  # Invalidate after this many consecutive failures


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class CacheEntry:
    """A cached set of selectors for a specific site + page."""

    domain: str
    page_path: str
    selectors: Dict[str, Any]
    confidence: float
    created_at: float
    ttl: int = DEFAULT_TTL
    failure_count: int = 0
    last_failure_at: Optional[float] = None
    last_success_at: Optional[float] = None
    hit_count: int = 0

    @property
    def is_expired(self) -> bool:
        return time.time() > (self.created_at + self.ttl)

    @property
    def is_invalidated(self) -> bool:
        return self.failure_count >= MAX_FAILURES

    @property
    def is_usable(self) -> bool:
        return not self.is_expired and not self.is_invalidated

    def record_success(self) -> None:
        self.failure_count = 0
        self.last_success_at = time.time()
        self.hit_count += 1

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_at = time.time()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "domain": self.domain,
            "page_path": self.page_path,
            "selectors": self.selectors,
            "confidence": self.confidence,
            "created_at": self.created_at,
            "ttl": self.ttl,
            "failure_count": self.failure_count,
            "last_failure_at": self.last_failure_at,
            "last_success_at": self.last_success_at,
            "hit_count": self.hit_count,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> CacheEntry:
        return cls(
            domain=data["domain"],
            page_path=data["page_path"],
            selectors=data["selectors"],
            confidence=data.get("confidence", 0.0),
            created_at=data["created_at"],
            ttl=data.get("ttl", DEFAULT_TTL),
            failure_count=data.get("failure_count", 0),
            last_failure_at=data.get("last_failure_at"),
            last_success_at=data.get("last_success_at"),
            hit_count=data.get("hit_count", 0),
        )


# ── Cache Key ─────────────────────────────────────────────────────────────────


def make_cache_key(domain: str, page_path: str) -> str:
    """Generate a deterministic cache key from domain + page path."""
    normalized = f"{domain.lower().strip()}/{page_path.strip('/')}"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


# ── Selector Cache ────────────────────────────────────────────────────────────


class SelectorCache:
    """In-memory selector cache with optional file persistence.

    Stores cached selectors keyed by (domain, page_path).
    Supports TTL expiration, failure tracking, and file persistence.
    """

    def __init__(self, persist_path: Optional[str] = None, ttl: int = DEFAULT_TTL):
        """
        Args:
            persist_path: Optional file path to persist cache to disk.
            ttl: Default time-to-live in seconds for new entries.
        """
        self._store: Dict[str, CacheEntry] = {}
        self._persist_path = persist_path
        self._default_ttl = ttl

        if persist_path:
            self._load_from_disk()

    def get(self, domain: str, page_path: str) -> Optional[CacheEntry]:
        """Look up cached selectors for a domain + page.

        Returns None if not cached, expired, or invalidated.
        """
        key = make_cache_key(domain, page_path)
        entry = self._store.get(key)

        if entry is None:
            return None

        if not entry.is_usable:
            logger.info(
                "Cache entry not usable: domain=%s path=%s expired=%s invalidated=%s",
                domain, page_path, entry.is_expired, entry.is_invalidated,
            )
            return None

        return entry

    def put(
        self,
        domain: str,
        page_path: str,
        selectors: Dict[str, Any],
        confidence: float = 0.0,
        ttl: Optional[int] = None,
    ) -> CacheEntry:
        """Store selectors in the cache.

        Args:
            domain: Website domain.
            page_path: Page URL path.
            selectors: CSS selector map from LLM extraction.
            confidence: LLM confidence score (0-1).
            ttl: Optional TTL override in seconds.

        Returns:
            The new CacheEntry.
        """
        key = make_cache_key(domain, page_path)
        entry = CacheEntry(
            domain=domain.lower().strip(),
            page_path=page_path,
            selectors=selectors,
            confidence=confidence,
            created_at=time.time(),
            ttl=ttl or self._default_ttl,
        )
        self._store[key] = entry
        logger.info(
            "Cached selectors: domain=%s path=%s fields=%d confidence=%.2f",
            domain, page_path, len(selectors), confidence,
        )

        if self._persist_path:
            self._save_to_disk()

        return entry

    def record_success(self, domain: str, page_path: str) -> None:
        """Record a successful extraction using cached selectors."""
        key = make_cache_key(domain, page_path)
        entry = self._store.get(key)
        if entry:
            entry.record_success()
            if self._persist_path:
                self._save_to_disk()

    def record_failure(self, domain: str, page_path: str) -> None:
        """Record a failed extraction using cached selectors."""
        key = make_cache_key(domain, page_path)
        entry = self._store.get(key)
        if entry:
            entry.record_failure()
            logger.warning(
                "Cache failure recorded: domain=%s path=%s count=%d/%d",
                domain, page_path, entry.failure_count, MAX_FAILURES,
            )
            if self._persist_path:
                self._save_to_disk()

    def invalidate(self, domain: str, page_path: str) -> bool:
        """Remove a specific cache entry.

        Returns True if an entry was removed.
        """
        key = make_cache_key(domain, page_path)
        if key in self._store:
            del self._store[key]
            if self._persist_path:
                self._save_to_disk()
            return True
        return False

    def invalidate_domain(self, domain: str) -> int:
        """Remove all cache entries for a domain.

        Returns the number of entries removed.
        """
        domain = domain.lower().strip()
        to_remove = [
            k for k, v in self._store.items() if v.domain == domain
        ]
        for k in to_remove:
            del self._store[k]
        if to_remove and self._persist_path:
            self._save_to_disk()
        return len(to_remove)

    def clear(self) -> None:
        """Remove all entries."""
        self._store.clear()
        if self._persist_path:
            self._save_to_disk()

    def stats(self) -> Dict[str, Any]:
        """Return cache statistics."""
        total = len(self._store)
        usable = sum(1 for e in self._store.values() if e.is_usable)
        expired = sum(1 for e in self._store.values() if e.is_expired)
        invalidated = sum(1 for e in self._store.values() if e.is_invalidated)
        total_hits = sum(e.hit_count for e in self._store.values())
        return {
            "total_entries": total,
            "usable": usable,
            "expired": expired,
            "invalidated": invalidated,
            "total_hits": total_hits,
        }

    @property
    def size(self) -> int:
        return len(self._store)

    def _save_to_disk(self) -> None:
        """Persist cache to a JSON file."""
        if not self._persist_path:
            return
        data = {k: v.to_dict() for k, v in self._store.items()}
        path = Path(self._persist_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    def _load_from_disk(self) -> None:
        """Load cache from a JSON file."""
        if not self._persist_path:
            return
        path = Path(self._persist_path)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for key, entry_data in data.items():
                self._store[key] = CacheEntry.from_dict(entry_data)
            logger.info("Loaded %d cache entries from disk", len(self._store))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load cache from disk: %s", e)
