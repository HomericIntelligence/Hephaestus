"""Read-through TTL cache for automation GraphQL reads (issue #3395).

The cache stops repeated identical reads from reaching GitHub when several
loop processes share one token budget. It lives at the ``run_graphql``
boundary: typed queries can be cached, typed mutations cannot.

Operation allow-lists (final, verified against the current tree):

- Cacheable: ``batchIssueStates``, ``BatchIssueLabels``, ``BatchIssueTitles``,
  ``batchDependencyFacts``, ``ScopeExpansionIssueOwner``,
  ``inlineReviewThreads``, ``reviewThreadSnapshot``,
  ``unresolvedReviewThreads``, ``pipelineThreadSnapshot``,
  ``PullRequestReviews``, ``reviewReceipts``.
- Bypass (always run upstream and is never saved): ``reviewedPrState``
  (exact-head review binding), ``issueComments``, ``BatchIssueComments``,
  ``issueCommentIds`` (first-publication state).
- Every other operation, including ``githubSchemaContract``, is not cached.
  Merge-gate, check-run, and rate-limit reads use REST and stay outside.

Key parts: token identity hash, repository (``owner/name`` from the call
variables), operation name, and the digest of the normalized variables. The
cross-process layer writes one owner-only JSON file per key under
``build/.issue_implementer/gh-read-cache/`` so loop processes on one host
share entries. No file holds a token, an authorization value, or a query
document.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hephaestus.io.utils import write_secure
from hephaestus.utils.cache import ThreadSafeCache
from hephaestus.utils.directory_mode import create_private_directory
from hephaestus.utils.file_lock import file_lock
from hephaestus.utils.helpers import get_repo_root

from ..git_runtime import get_repo_info
from ..models import DEFAULT_STATE_DIR

if TYPE_CHECKING:
    from .graphql import GraphQLScalar

logger = logging.getLogger(__name__)

CACHEABLE_OPERATIONS = frozenset(
    {
        "batchIssueStates",
        "BatchIssueLabels",
        "BatchIssueTitles",
        "batchDependencyFacts",
        "ScopeExpansionIssueOwner",
        "inlineReviewThreads",
        "reviewThreadSnapshot",
        "unresolvedReviewThreads",
        "pipelineThreadSnapshot",
        "PullRequestReviews",
        "reviewReceipts",
    }
)
"""Operations whose validated results the cache may save."""

BYPASS_OPERATIONS = frozenset(
    {
        "reviewedPrState",
        "issueComments",
        "BatchIssueComments",
        "issueCommentIds",
    }
)
"""Freshness bypass points: always read upstream, never serve stale."""

_ENTRY_SCHEMA = 1
_ANONYMOUS_SCOPE = "hephaestus-anonymous"
_LOCK_DIR_NAME = "locks"
_WRITE_LOCK_NAME = "write.lock"


class _CacheMissError(RuntimeError):
    """Internal: a file-layer lookup found no valid entry."""


@dataclass(frozen=True)
class ReadKey:
    """Identity of one cached read."""

    token: str
    """Truncated SHA-256 of the token. The token itself is never saved."""

    repository: str
    """``owner/name`` from the call variables."""

    operation: str
    """The GraphQLQuerySpec operation name."""

    digest: str
    """Truncated SHA-256 of the normalized variables."""


def _default_token_identity() -> str:
    """Return the token that ``gh`` uses, or an empty string when unknown."""
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        value = os.environ.get(name)
        if value:
            return value
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    token = result.stdout.strip()
    return token if result.returncode == 0 else ""


def _sanitize_component(value: str) -> str:
    """Make one filename component safe: keep alphanumerics, ``._-`` only."""
    return "".join(char if (char.isalnum() or char in "._-") else "_" for char in value)


def _normalize_variables(variables: Mapping[str, GraphQLScalar]) -> str | None:
    """Return the canonical JSON digest input, or None to fail open.

    List order is preserved and object keys are sorted. A non-scalar value
    makes the read uncacheable (fail open).
    """
    for value in variables.values():
        if not isinstance(value, (int, str)) or isinstance(value, bool):
            return None
    return json.dumps(dict(variables), sort_keys=True, separators=(",", ":"))


class GraphQLReadCache:
    """Read-through cache in front of the automation GraphQL transport.

    The process layer is one :class:`ThreadSafeCache`; the host layer is one
    owner-only JSON file per key. TTL expiry uses the injected monotonic clock
    in-process and wall-clock epochs in files so several processes can share
    entries. All file-layer failures degrade to a plain upstream read with one
    logged warning; the read never fails because of the cache.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_entries: int,
        directory: Path,
        token_identity: Callable[[], str] = _default_token_identity,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        """Create a cache; ``token_identity`` supplies the secret to hash.

        Args:
            ttl_seconds: Entry time-to-live. Values at or below zero expire at
                once (the configure seam disables the cache instead).
            max_entries: Bound on in-process entries and on entry-file count.
            directory: Owner-only base directory for entry files.
            token_identity: Zero-argument callable returning token material.
                Called once lazily; only its truncated hash is retained.
            clock: Monotonic clock for the process layer.
            wall_clock: Wall clock for entry-file expiry across processes.

        """
        self._ttl = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._directory = Path(directory)
        self._token_identity = token_identity
        self._wall_clock = wall_clock
        self._process: ThreadSafeCache[ReadKey, Any] = ThreadSafeCache(
            ttl_seconds=self._ttl,
            clock=clock,
        )
        self._lock = threading.Lock()
        self._order: deque[ReadKey] = deque()
        self._order_set: set[ReadKey] = set()
        self._token_hash: str | None = None
        self._hits = 0
        self._misses = 0
        self._bypasses = 0
        self._evictions = 0

    # ------------------------------------------------------------------
    # Public boundary API used by run_graphql and labels.py
    # ------------------------------------------------------------------

    def is_cacheable_operation(self, operation: str) -> bool:
        """Return whether the cache may save this operation's result."""
        return operation in CACHEABLE_OPERATIONS

    def is_bypass_operation(self, operation: str) -> bool:
        """Return whether this operation is a freshness bypass point."""
        return operation in BYPASS_OPERATIONS

    def note_bypass(self) -> None:
        """Count one bypassed read that ran upstream."""
        with self._lock:
            self._bypasses += 1

    def build_key(self, operation: str, variables: Mapping[str, GraphQLScalar]) -> ReadKey | None:
        """Build the read key, or None to fail open (do not cache).

        The repository identity comes from the explicit ``owner``/``name``
        call variables so multi-repo loops never mix identities.
        """
        owner = variables.get("owner")
        name = variables.get("name")
        if not isinstance(owner, str) or not isinstance(name, str) or not owner or not name:
            return None
        normalized = _normalize_variables(variables)
        if normalized is None:
            return None
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]
        return ReadKey(
            token=self._token_scope(),
            repository=f"{owner}/{name}",
            operation=operation,
            digest=digest,
        )

    def get(self, key: ReadKey) -> tuple[bool, Any | None]:
        """Return ``(hit, value)`` for *key*; a hit is a defensive copy."""
        try:
            value = self._process.get_or_compute(key, lambda: self._read_entry_file(key))
        except _CacheMissError:
            with self._lock:
                self._misses += 1
            return False, None
        with self._lock:
            self._hits += 1
        return True, copy.deepcopy(value)

    def store(self, key: ReadKey, value: Any) -> None:
        """Save a validated result in the process layer and the file layer."""
        stored = copy.deepcopy(value)
        self._process.get_or_compute(key, lambda: stored)
        self._track_entry(key)
        self._write_entry_file(key, stored)

    def remove_key(self, key: ReadKey | None) -> None:
        """Drop one key from both layers; a bypass operation stays unserved."""
        if key is None:
            return
        self._process.remove(key)
        self._forget_entry(key)
        try:
            self._entry_path(key).unlink(missing_ok=True)
        except OSError as exc:
            self._log_file_issue("remove", exc)

    def invalidate_repository(self, repository: str) -> None:
        """Drop every entry this token scope holds for *repository*."""
        if "/" not in repository:
            self.invalidate_token_scope()
            return
        owner, name = repository.split("/", 1)
        prefix = (
            f"{_sanitize_component(self._token_scope())}."
            f"{_sanitize_component(owner)}.{_sanitize_component(name)}."
        )
        token = self._token_scope()
        with self._lock:
            dropped = [
                key for key in self._order if key.token == token and key.repository == repository
            ]
        for key in dropped:
            self._process.remove(key)
            self._forget_entry(key)
        self._unlink_matching(prefix)

    def invalidate_token_scope(self) -> None:
        """Drop every entry this token scope holds (repository unknown)."""
        token = self._token_scope()
        with self._lock:
            dropped = [key for key in self._order if key.token == token]
        for key in dropped:
            self._process.remove(key)
            self._forget_entry(key)
        self._unlink_matching(f"{_sanitize_component(token)}.")

    def invalidate_current_repository(self) -> None:
        """Invalidate the ambient repository; fall back to the token scope."""
        try:
            owner, name = get_repo_info()
        except RuntimeError:
            logger.warning(
                "Could not resolve the repository for cache invalidation; clearing the token scope"
            )
            self.invalidate_token_scope()
            return
        self.invalidate_repository(f"{owner}/{name}")

    def stats(self) -> dict[str, int | float]:
        """Return the event-log counter snapshot (JSON-safe)."""
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "bypasses": self._bypasses,
                "evictions": self._evictions,
                "entries": len(self._order),
                "ttl_seconds": int(self._ttl),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _token_scope(self) -> str:
        """Return the truncated SHA-256 of the token, resolved once."""
        with self._lock:
            if self._token_hash is None:
                try:
                    material = self._token_identity()
                except Exception:
                    logger.exception("token identity resolution failed")
                    material = ""
                digest_input = material if material else _ANONYMOUS_SCOPE
                self._token_hash = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
            return self._token_hash

    def _entry_path(self, key: ReadKey) -> Path:
        """Return the entry-file path for *key*."""
        owner, name = key.repository.split("/", 1)
        filename = (
            f"{_sanitize_component(key.token)}."
            f"{_sanitize_component(owner)}.{_sanitize_component(name)}."
            f"{_sanitize_component(key.operation)}.{key.digest}.json"
        )
        return self._directory / filename

    def _track_entry(self, key: ReadKey) -> None:
        """Remember *key* and evict the oldest entry above the bound."""
        with self._lock:
            if key in self._order_set:
                return
            self._order.append(key)
            self._order_set.add(key)
            while len(self._order) > self._max_entries:
                oldest = self._order.popleft()
                self._order_set.discard(oldest)
                self._process.remove(oldest)
                self._evictions += 1

    def _forget_entry(self, key: ReadKey) -> None:
        """Drop *key* from the ordered bound list."""
        with self._lock:
            if key not in self._order_set:
                return
            self._order_set.discard(key)
            with suppress(ValueError):
                self._order.remove(key)

    def _fresh_expiry(self) -> float:
        """Return the wall-clock expiry for a new entry."""
        return self._wall_clock() + self._ttl

    def _write_entry_file(self, key: ReadKey, value: Any) -> None:
        """Atomically write the owner-only entry file, then bound the files."""
        record = {
            "schema": _ENTRY_SCHEMA,
            "expires_at": self._fresh_expiry(),
            "repository": key.repository,
            "operation": key.operation,
            "value": value,
        }
        try:
            create_private_directory(self._directory, parents=True)
            lock_path = self._directory / _LOCK_DIR_NAME / _WRITE_LOCK_NAME
            with file_lock(lock_path):
                write_secure(self._entry_path(key), json.dumps(record, sort_keys=True))
                self._sweep_entry_files()
        except OSError as exc:
            self._log_file_issue("write", exc)

    def _sweep_entry_files(self) -> None:
        """Delete the oldest entry files above the entry-file bound."""
        entries = [path for path in self._directory.glob("*.json") if path.is_file()]
        if len(entries) <= self._max_entries:
            return
        entries.sort(key=lambda path: (path.stat().st_mtime, path.name))
        excess = len(entries) - self._max_entries
        for path in entries[:excess]:
            path.unlink(missing_ok=True)
        with self._lock:
            self._evictions += excess

    def _read_entry_file(self, key: ReadKey) -> Any:
        """Return the file-layer value for *key* or raise ``_CacheMissError``."""
        path = self._entry_path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            raise _CacheMissError(str(path)) from None
        try:
            record = json.loads(raw)
            valid_shape = (
                isinstance(record, dict)
                and record.get("schema") == _ENTRY_SCHEMA
                and record.get("repository") == key.repository
                and record.get("operation") == key.operation
                and isinstance(record.get("expires_at"), (int, float))
                and not isinstance(record.get("expires_at"), bool)
                and "value" in record
            )
        except (json.JSONDecodeError, AttributeError):
            valid_shape = False
        if not valid_shape:
            logger.warning("Ignoring malformed read-cache entry file: %s", path)
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise _CacheMissError(str(path))
        expires_at = float(record["expires_at"])
        if self._wall_clock() >= expires_at:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise _CacheMissError(str(path))
        return record["value"]

    def _unlink_matching(self, prefix: str) -> None:
        """Unlink entry files whose names start with *prefix* under the lock."""
        try:
            lock_path = self._directory / _LOCK_DIR_NAME / _WRITE_LOCK_NAME
            with file_lock(lock_path):
                for path in self._directory.glob(f"{prefix}*.json"):
                    path.unlink(missing_ok=True)
        except OSError as exc:
            self._log_file_issue("invalidate", exc)

    def _log_file_issue(self, action: str, exc: OSError) -> None:
        """Degrade the file layer with one warning; the read goes upstream."""
        logger.warning(
            "read-cache entry-file %s failed in %s; continuing without the file layer: %s",
            action,
            self._directory,
            exc,
        )


def configure_github_read_cache(
    *,
    ttl_seconds: float,
    max_entries: int,
    directory: Path,
    token_identity: Callable[[], str] = _default_token_identity,
    clock: Callable[[], float] = time.monotonic,
    wall_clock: Callable[[], float] = time.time,
) -> GraphQLReadCache | None:
    """Create and bind the process-wide read cache; None disables it.

    A TTL of zero disables the cache (the operator default is one hour).
    """
    cache: GraphQLReadCache | None
    if ttl_seconds <= 0:
        cache = None
    else:
        cache = GraphQLReadCache(
            ttl_seconds=ttl_seconds,
            max_entries=max_entries,
            directory=directory,
            token_identity=token_identity,
            clock=clock,
            wall_clock=wall_clock,
        )
    package = sys.modules.get("hephaestus.automation.github_api")
    if package is not None:
        package._gh_read_cache = cache  # type: ignore[attr-defined]
    return cache


def configured_read_cache() -> GraphQLReadCache | None:
    """Return the process-wide cache, or None when disabled or unbound."""
    package = sys.modules.get("hephaestus.automation.github_api")
    if package is None:
        return None
    cache = getattr(package, "_gh_read_cache", None)
    return cache if isinstance(cache, GraphQLReadCache) else None


def configure_github_read_cache_from_args(args: argparse.Namespace) -> GraphQLReadCache | None:
    """Apply ``--gh-cache-*`` options parsed from CLI args.

    This helper lives in the automation layer: ADR-0001 forbids the library
    ``hephaestus.cli`` package from importing it.
    """
    ttl = int(getattr(args, "gh_cache_ttl", 0) or 0)
    max_entries = int(getattr(args, "gh_cache_max_entries", 2000) or 2000)
    raw_directory = getattr(args, "gh_cache_dir", None)
    if raw_directory:
        directory = Path(raw_directory).expanduser()
    else:
        directory = get_repo_root() / DEFAULT_STATE_DIR / "gh-read-cache"
    return configure_github_read_cache(
        ttl_seconds=ttl,
        max_entries=max_entries,
        directory=directory,
    )
