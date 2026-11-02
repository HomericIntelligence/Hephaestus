"""Behavior tests for the GitHub GraphQL read-through cache (#3395)."""

from __future__ import annotations

import json
import multiprocessing
import stat
import time
from pathlib import Path
from typing import Any

import hephaestus.automation.github_api as github_api
from hephaestus.automation.github_api.cache import (
    BYPASS_OPERATIONS,
    CACHEABLE_OPERATIONS,
    GraphQLReadCache,
    ReadKey,
    configure_github_read_cache,
)
from hephaestus.automation.pipeline.coordinator_observability import record_event


class _Clocks:
    """Controllable monotonic and wall clocks for deterministic TTL tests."""

    def __init__(self) -> None:
        self.monotonic_now = 0.0
        self.wall_now = 1_000_000.0

    def monotonic(self) -> float:
        return self.monotonic_now

    def wall(self) -> float:
        return self.wall_now

    def advance(self, seconds: float) -> None:
        self.monotonic_now += seconds
        self.wall_now += seconds


def make_cache(
    tmp_path: Path,
    *,
    clocks: _Clocks | None = None,
    ttl: float = 3600.0,
    max_entries: int = 2000,
    token: str = "test-token",
) -> GraphQLReadCache:
    """Return a cache bound to *tmp_path* with injectable seams."""
    clocks = clocks or _Clocks()
    return GraphQLReadCache(
        ttl_seconds=ttl,
        max_entries=max_entries,
        directory=tmp_path / "gh-read-cache",
        token_identity=lambda: token,
        clock=clocks.monotonic,
        wall_clock=clocks.wall,
    )


def make_key(cache: GraphQLReadCache, n0: int = 1) -> ReadKey:
    """Build one cacheable key with repository scope o/r."""
    key = cache.build_key("batchIssueStates", {"owner": "o", "name": "r", "n0": n0})
    assert key is not None
    return key


def test_allow_lists_match_the_reviewed_final_sets() -> None:
    """The allow-lists pin the plan-reviewed operation names."""
    assert "batchIssueStates" in CACHEABLE_OPERATIONS
    assert "reviewReceipts" in CACHEABLE_OPERATIONS
    assert "reviewedPrState" in BYPASS_OPERATIONS
    assert "issueComments" in BYPASS_OPERATIONS
    assert "BatchIssueComments" in BYPASS_OPERATIONS
    assert "issueCommentIds" in BYPASS_OPERATIONS
    assert not CACHEABLE_OPERATIONS.intersection(BYPASS_OPERATIONS)


def test_repeated_get_serves_one_upstream_store(tmp_path: Path) -> None:
    """A stored value serves repeats within the TTL without a new compute."""
    cache = make_cache(tmp_path)
    key = make_key(cache)
    cache.store(key, {"ok": 1})
    hit, first = cache.get(key)
    assert hit
    assert first == {"ok": 1}
    hit, second = cache.get(key)
    assert hit
    assert second == first
    assert cache.stats()["hits"] == 2


def test_ttl_expiry_forces_a_new_upstream_read(tmp_path: Path) -> None:
    """Both layers expire after the TTL: the next get reports a miss."""
    clocks = _Clocks()
    cache = make_cache(tmp_path, clocks=clocks)
    key = make_key(cache)
    cache.store(key, {"ok": 1})
    assert cache.get(key)[0]
    clocks.advance(3601.0)
    hit, _ = cache.get(key)
    assert not hit
    assert cache.stats()["misses"] == 1


def test_entry_file_survives_a_process_layer_restart(tmp_path: Path) -> None:
    """A second cache instance shares the entry files of the first one."""
    clocks = _Clocks()
    first = make_cache(tmp_path, clocks=clocks)
    key = make_key(first)
    first.store(key, {"ok": 1})
    second = make_cache(tmp_path, clocks=clocks)
    hit, value = second.get(key)
    assert hit
    assert value == {"ok": 1}


def test_hit_returns_a_defensive_copy(tmp_path: Path) -> None:
    """A caller cannot change the shared cached value."""
    cache = make_cache(tmp_path)
    key = make_key(cache)
    cache.store(key, {"nested": {"count": 1}})
    hit, value = cache.get(key)
    assert hit
    assert isinstance(value, dict)
    value["nested"]["count"] = 99
    hit, again = cache.get(key)
    assert hit
    assert again == {"nested": {"count": 1}}


def test_mutation_scope_invalidation_removes_repository_entries(tmp_path: Path) -> None:
    """invalidate_repository drops process and file entries for that repo."""
    cache = make_cache(tmp_path)
    key = make_key(cache)
    other = cache.build_key("batchIssueStates", {"owner": "o", "name": "other", "n0": 1})
    assert other is not None
    cache.store(key, {"ok": 1})
    cache.store(other, {"ok": 2})
    cache.invalidate_repository("o/r")
    assert not cache.get(key)[0]
    assert cache.get(other)[0]


def test_token_scope_invalidation_clears_everything(tmp_path: Path) -> None:
    """invalidate_token_scope drops all repositories of this token."""
    cache = make_cache(tmp_path)
    first = make_key(cache, n0=1)
    second = cache.build_key("batchIssueStates", {"owner": "o", "name": "other", "n0": 1})
    assert second is not None
    cache.store(first, {"ok": 1})
    cache.store(second, {"ok": 2})
    cache.invalidate_token_scope()
    assert not cache.get(first)[0]
    assert not cache.get(second)[0]


def test_non_scalar_variable_fails_open(tmp_path: Path) -> None:
    """A list-typed variable makes the read uncacheable, not an error."""
    cache = make_cache(tmp_path)
    key = cache.build_key("batchIssueStates", {"owner": "o", "name": "r", "tags": ["x"]})  # type: ignore[dict-item]
    assert key is None


def test_missing_repository_identity_fails_open(tmp_path: Path) -> None:
    """Node-id-only queries carry no repository and are not cached."""
    cache = make_cache(tmp_path)
    assert cache.build_key("reviewedPrState", {"id": "PR_x"}) is None


def test_unlisted_operation_is_not_cached(tmp_path: Path) -> None:
    """An operation name in neither allow-list is never cacheable."""
    cache = make_cache(tmp_path)
    assert cache.is_cacheable_operation("githubSchemaContract") is False
    assert cache.is_cacheable_operation("someNewOperation") is False
    assert cache.is_bypass_operation("someNewOperation") is False


def test_entry_files_are_owner_only_and_free_of_token_text(tmp_path: Path) -> None:
    """AC4: files and directories are owner-only; no token text is written."""
    secret = "sekret-token-material"  # noqa: S105 - sentinel, not a credential
    cache = make_cache(tmp_path, token=secret)
    key = make_key(cache)
    cache.store(key, {"ok": 1})
    directory = tmp_path / "gh-read-cache"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    files = list(directory.glob("*.json"))
    assert files
    for path in files:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert secret not in path.read_text(encoding="utf-8")
        assert secret not in path.name


def test_malformed_entry_file_is_removed_and_treated_as_miss(tmp_path: Path) -> None:
    """A malformed entry invalidates that single key and re-reads upstream."""
    cache = make_cache(tmp_path)
    key = make_key(cache)
    cache.store(key, {"ok": 1})
    cache._entry_path(key).write_text("not json", encoding="utf-8")
    # Drop the in-process copy so the file layer is exercised.
    cache._process.clear()
    hit, _ = cache.get(key)
    assert not hit
    assert not cache._entry_path(key).exists()


def test_expired_entry_file_is_removed(tmp_path: Path) -> None:
    """An expired file does not serve a stale value."""
    clocks = _Clocks()
    first = make_cache(tmp_path, clocks=clocks)
    key = make_key(first)
    first.store(key, {"ok": 1})
    clocks.advance(3601.0)
    second = make_cache(tmp_path, clocks=clocks)
    hit, _ = second.get(key)
    assert not hit
    assert not second._entry_path(key).exists()


def test_entry_bound_evicts_the_oldest_process_entry(tmp_path: Path) -> None:
    """The process cache and entry-file count stay within the bound."""
    cache = make_cache(tmp_path, max_entries=2)
    keys = [make_key(cache, n0=idx) for idx in range(3)]
    for idx, key in enumerate(keys):
        cache.store(key, {"ok": idx})
    stats = cache.stats()
    assert stats["entries"] == 2
    assert stats["evictions"] >= 1
    # The oldest entry was evicted from the process layer.
    assert not cache.get(keys[0])[0]
    # File-layer count stays bounded as well.
    files = list((tmp_path / "gh-read-cache").glob("*.json"))
    assert len(files) <= 2


def test_zero_ttl_disables_the_cache(tmp_path: Path) -> None:
    """A zero TTL binds no cache; the default-off posture is kept."""
    cache = configure_github_read_cache(
        ttl_seconds=0,
        max_entries=10,
        directory=tmp_path / "gh-read-cache",
    )
    assert cache is None
    assert github_api._gh_read_cache is None


def _child_cache_writer(directory: str, loops: int) -> None:
    """Store and read entries from a child process against the shared store."""
    cache = GraphQLReadCache(
        ttl_seconds=60.0,
        max_entries=500,
        directory=Path(directory),
        token_identity=lambda: "shared-token",
    )
    for idx in range(loops):
        key = cache.build_key("batchIssueStates", {"owner": "o", "name": "r", "n0": idx})
        assert key is not None
        cache.store(key, {"idx": idx})
        hit, value = cache.get(key)
        assert hit
        assert value == {"idx": idx}


def test_two_processes_share_entries_safely(tmp_path: Path) -> None:
    """AC4: interleaved writers leave only complete, parseable entries."""
    directory = tmp_path / "gh-read-cache"
    loops = 20
    context = multiprocessing.get_context("spawn")
    children = [
        context.Process(target=_child_cache_writer, args=(str(directory), loops)) for _ in range(2)
    ]
    for child in children:
        child.start()
    for child in children:
        child.join(timeout=60)
    for child in children:
        assert child.exitcode == 0
    files = list(directory.glob("*.json"))
    assert len(files) == loops
    for path in files:
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["schema"] == 1
        assert isinstance(record["value"]["idx"], int)


class _FakeCoordinatorConfig:
    def __init__(self, event_log_path: Path) -> None:
        self.event_log_path = event_log_path
        self.event_log_handle = None


class _FakeCoordinator:
    def __init__(self, event_log_path: Path) -> None:
        self.event_log: list[tuple[str, Any]] = []
        self._event_log_disabled = False
        self.config = _FakeCoordinatorConfig(event_log_path)


def test_stats_event_records_ac5_counters(tmp_path: Path) -> None:
    """AC5: one gh_read_cache_stats record carries the hit/miss counters."""
    cache = make_cache(tmp_path)
    key = make_key(cache)
    reads = 3  # one store-serving flow plus two hits
    cache.store(key, {"ok": 1})
    for _ in range(reads - 1):
        assert cache.get(key)[0]
    stats = cache.stats()
    assert stats["hits"] == reads - 1

    event_log_path = tmp_path / "events.jsonl"
    coordinator = _FakeCoordinator(event_log_path)
    record_event(
        coordinator,
        "gh_read_cache_stats",
        stats,
        now_fn=time.time,
        logger=github_api.logger,
    )
    lines = event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "gh_read_cache_stats"
    assert record["fields"][0]["hits"] == reads - 1
    assert record["fields"][0]["misses"] == 0
    assert record["fields"][0]["bypasses"] == 0
