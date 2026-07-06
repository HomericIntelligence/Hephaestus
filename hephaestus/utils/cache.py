"""Thread-safe TTL cache for memoizing per-key computed values.

Replaces ad-hoc module-level ``dict`` caches that have a TOCTOU race between
the membership check and the assignment, and that never expire. Used by
``hephaestus.automation.git_utils`` for repo-info/slug memoization.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")

DEFAULT_TTL_SECONDS = 300.0


class ThreadSafeCache(Generic[K, V]):
    """A lock-protected cache with per-entry TTL expiry.

    Entries expire ``ttl_seconds`` after they are stored (measured with
    ``time.monotonic`` so it is immune to wall-clock changes). All cache
    access is guarded by a single ``threading.Lock``.

    The ``compute`` callable in :meth:`get_or_compute` runs *outside* the lock
    so a slow producer for one key does not block readers/writers of other
    keys. The deliberate consequence is that several threads racing the SAME
    cold key may each run ``compute`` once (a bounded, idempotent-read
    thundering herd); the final store is last-writer-wins and all callers
    receive an equal value. A ``compute`` that raises propagates and stores
    nothing — failures are never memoized, matching the prior dict-cache
    behavior of only caching successes.
    """

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        """Create an empty cache whose entries expire after ``ttl_seconds``."""
        self._cache: dict[K, tuple[V, float]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl_seconds

    def get_or_compute(self, key: K, compute: Callable[[], V]) -> V:
        """Return the cached value for *key*, or compute, store, and return it.

        On a fresh hit the cached value is returned without calling *compute*.
        On a miss or expired entry, *compute* is called outside the lock and the
        result stored. If *compute* raises, the exception propagates and nothing
        is cached.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                value, ts = entry
                if time.monotonic() - ts < self._ttl:
                    return value
        value = compute()
        with self._lock:
            self._cache[key] = (value, time.monotonic())
        return value

    def clear(self) -> None:
        """Remove all entries. For test isolation and long-lived processes."""
        with self._lock:
            self._cache.clear()

    def add_to_entry(self, key: K, item: Any) -> None:
        """If *key* holds a set entry, add *item* to it under the lock.

        No-op when the key is absent — mirrors the label cache's "only
        augment an already-populated entry" contract. This does not check TTL
        expiry, so an entry past its TTL is still augmented; the stale
        augmented entry is replaced on the next ``get_or_compute`` call. TTL
        timestamp is preserved (augmenting does not refresh expiry).

        Intended for use with ``ThreadSafeCache[K, set[V]]`` where adding an
        element to a cached set is atomic (label cache in github_api).
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                value, ts = entry
                self._cache[key] = ({*value, item}, ts)  # type: ignore[assignment,misc]

    def remove(self, key: K) -> None:
        """Remove *key* from the cache. No-op if the key is absent."""
        with self._lock:
            self._cache.pop(key, None)
