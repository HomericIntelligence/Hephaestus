"""Tests for the thread-safe TTL cache utility."""

import threading
from unittest.mock import Mock

import pytest

from hephaestus.utils.cache import DEFAULT_TTL_SECONDS, ThreadSafeCache


def test_miss_computes_and_returns() -> None:
    """A cold key computes its value and returns it."""
    cache: ThreadSafeCache[str, int] = ThreadSafeCache()
    assert cache.get_or_compute("k", lambda: 42) == 42


def test_warm_hit_reuses_without_recompute() -> None:
    """A second call with the same fresh key returns the cached object without recomputing."""
    cache: ThreadSafeCache[str, object] = ThreadSafeCache()
    sentinel = object()
    compute = Mock(return_value=sentinel)

    first = cache.get_or_compute("k", compute)
    second = cache.get_or_compute("k", compute)

    assert first is sentinel
    assert second is sentinel
    assert compute.call_count == 1


def test_ttl_expiry_recomputes(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry older than the TTL is recomputed on the next access."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("hephaestus.utils.cache.time.monotonic", lambda: clock["now"])

    cache: ThreadSafeCache[str, int] = ThreadSafeCache(ttl_seconds=10.0)
    compute = Mock(side_effect=[1, 2])

    assert cache.get_or_compute("k", compute) == 1
    clock["now"] += 11.0  # past the TTL
    assert cache.get_or_compute("k", compute) == 2
    assert compute.call_count == 2


def test_ttl_not_expired_keeps_hit(monkeypatch: pytest.MonkeyPatch) -> None:
    """An entry younger than the TTL is served from the cache without recomputing."""
    clock = {"now": 1000.0}
    monkeypatch.setattr("hephaestus.utils.cache.time.monotonic", lambda: clock["now"])

    cache: ThreadSafeCache[str, int] = ThreadSafeCache(ttl_seconds=10.0)
    compute = Mock(side_effect=[1, 2])

    assert cache.get_or_compute("k", compute) == 1
    clock["now"] += 5.0  # still within the TTL
    assert cache.get_or_compute("k", compute) == 1
    assert compute.call_count == 1


def test_exceptions_are_not_cached() -> None:
    """A raising compute propagates and stores nothing; a later compute runs."""
    cache: ThreadSafeCache[str, int] = ThreadSafeCache()

    def boom() -> int:
        raise RuntimeError("compute failed")

    with pytest.raises(RuntimeError, match="compute failed"):
        cache.get_or_compute("k", boom)

    # Nothing was memoized, so a now-succeeding compute runs and is stored.
    assert cache.get_or_compute("k", lambda: 7) == 7


def test_clear_empties_cache() -> None:
    """After clear(), the next access recomputes."""
    cache: ThreadSafeCache[str, int] = ThreadSafeCache()
    compute = Mock(side_effect=[1, 2])

    assert cache.get_or_compute("k", compute) == 1
    cache.clear()
    assert cache.get_or_compute("k", compute) == 2
    assert compute.call_count == 2


def test_default_ttl_is_300_seconds() -> None:
    """The module-level default TTL is 300 seconds."""
    assert DEFAULT_TTL_SECONDS == 300.0
    assert ThreadSafeCache()._ttl == DEFAULT_TTL_SECONDS


def test_concurrency_same_key_no_crash_equal_values() -> None:
    """N threads racing the same cold key all return equal values without crashing.

    Because ``compute`` runs outside the lock, racers may each run it once, so
    we assert equality (not object identity) and that no thread raised.
    """
    num_threads = 16
    cache: ThreadSafeCache[str, tuple[str, str]] = ThreadSafeCache()
    barrier = threading.Barrier(num_threads)
    results: list[tuple[str, str]] = []
    errors: list[Exception] = []
    results_lock = threading.Lock()

    def slow_compute() -> tuple[str, str]:
        # Fresh-but-equal value each call so equality holds without identity.
        chars = ["o", "r"]
        return (chars[0], chars[1])

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            value = cache.get_or_compute("same", slow_compute)
            with results_lock:
                results.append(value)
        except Exception as exc:
            with results_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == num_threads
    assert all(value == ("o", "r") for value in results)


def test_concurrency_distinct_keys_no_lost_writes() -> None:
    """N threads each populate a distinct key; the locked store loses no writes."""
    num_threads = 32
    cache: ThreadSafeCache[int, int] = ThreadSafeCache()
    barrier = threading.Barrier(num_threads)
    errors: list[Exception] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=5)
            cache.get_or_compute(i, lambda: i)
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    # Every key is present afterward with its correct value (no lost writes).
    observed = {i: cache.get_or_compute(i, lambda: -1) for i in range(num_threads)}
    assert observed == {i: i for i in range(num_threads)}


def test_add_to_entry_augments_existing_set() -> None:
    """add_to_entry adds an item to an existing set-typed entry."""
    c: ThreadSafeCache[str, set[str]] = ThreadSafeCache()
    c.get_or_compute("k", lambda: {"a"})
    c.add_to_entry("k", "b")
    assert c.get_or_compute("k", set) == {"a", "b"}


def test_add_to_entry_noop_when_key_absent() -> None:
    """add_to_entry is a no-op when the key is absent."""
    c: ThreadSafeCache[str, set[str]] = ThreadSafeCache()
    c.add_to_entry("missing", "b")
    # No entry created; accessing the key triggers a fresh compute.
    assert c.get_or_compute("missing", lambda: {"x"}) == {"x"}


def test_remove_deletes_entry() -> None:
    """remove(key) deletes the entry; subsequent access recomputes."""
    c: ThreadSafeCache[str, int] = ThreadSafeCache()
    compute = Mock(side_effect=[1, 2])

    assert c.get_or_compute("k", compute) == 1
    c.remove("k")
    assert c.get_or_compute("k", compute) == 2
    assert compute.call_count == 2


def test_remove_noop_when_key_absent() -> None:
    """remove(key) is a no-op when the key is absent."""
    c: ThreadSafeCache[str, int] = ThreadSafeCache()
    c.remove("missing")  # should not raise
    assert c.get_or_compute("missing", lambda: 42) == 42
