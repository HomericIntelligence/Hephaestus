"""Durable receipts for detached-review checkouts preserved after remote drift."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.io.utils import write_secure
from hephaestus.utils.file_lock import file_lock

_RECEIPT_DIR = "direct-review-recovery"
_RECEIPT_VERSION = 1
_REMOTE_CHANGED_REASON = "remote_changed"


def _is_full_sha(value: object) -> bool:
    """Return whether *value* is a full SHA-1 or SHA-256 commit identifier."""
    return (
        isinstance(value, str)
        and len(value) in (40, 64)
        and all(char in "0123456789abcdef" for char in value)
    )


def _validated_worktree_path(repo_root: Path, issue: int, path: Path) -> Path:
    """Return a normalized isolated-review path or raise for an unsafe receipt."""
    if isinstance(issue, bool) or not isinstance(issue, int) or issue <= 0:
        raise ValueError("direct review recovery issue is invalid")
    root = (repo_root / "build" / ".worktrees").resolve()
    normalized = path.resolve()
    prefix = f"review-pr-{issue}"
    suffix = normalized.name.removeprefix(prefix)
    if (
        normalized.parent != root
        or not normalized.name.startswith(prefix)
        or (suffix and (not suffix.startswith("-") or not suffix[1:].isdigit()))
    ):
        raise ValueError("direct review recovery worktree is invalid")
    return normalized


def _receipt_dir(repo_root: Path) -> Path:
    """Return the repository-scoped direct-review receipt directory."""
    return repo_root / DEFAULT_STATE_DIR / _RECEIPT_DIR


def _receipt_path(receipt_dir: Path, pr: int, worktree: Path) -> Path:
    """Return a stable path for one PR/worktree recovery receipt."""
    digest = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    return receipt_dir / f"direct-review-{pr}-{digest}.json"


def _receipt_lock_path(receipt_dir: Path, pr: int) -> Path:
    """Return the per-PR lock serializing receipt writes and reads."""
    return receipt_dir / f"direct-review-{pr}.lock"


def record_direct_review_recovery(
    *,
    repo_root: Path,
    issue: int,
    pr: int,
    worktree: Path,
    branch: str,
    expected_remote_sha: str,
    source_sha: str,
) -> Path:
    """Persist an immutable receipt for a verified detached-push remote drift.

    A receipt is written only after the remote has authoritatively changed, so
    later runs can distinguish an abandoned recovery checkout from a merely
    occupied (and potentially active) checkout.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        raise ValueError("direct review recovery PR is invalid")
    if not isinstance(branch, str) or not branch:
        raise ValueError("direct review recovery branch is invalid")
    if not _is_full_sha(expected_remote_sha) or not _is_full_sha(source_sha):
        raise ValueError("direct review recovery commit receipt is invalid")
    normalized_root = repo_root.resolve()
    normalized_worktree = _validated_worktree_path(normalized_root, issue, worktree)
    if not normalized_worktree.is_dir():
        raise ValueError("direct review recovery worktree is missing")
    receipt_dir = _receipt_dir(normalized_root)
    receipt = {
        "branch": branch,
        "expected_remote_sha": expected_remote_sha,
        "issue": issue,
        "pr": pr,
        "reason": _REMOTE_CHANGED_REASON,
        "repo_root": str(normalized_root),
        "schema_version": _RECEIPT_VERSION,
        "source_sha": source_sha,
        "worktree": str(normalized_worktree),
    }
    target = _receipt_path(receipt_dir, pr, normalized_worktree)
    with file_lock(_receipt_lock_path(receipt_dir, pr)):
        write_secure(target, json.dumps(receipt, sort_keys=True) + "\n")
    return target


def list_direct_review_recovery_paths(*, repo_root: Path, issue: int, pr: int) -> list[Path]:
    """Return only valid, receipt-backed detached-review recovery paths.

    Invalid or tampered receipts are ignored. They are not evidence that an
    arbitrary occupied checkout is safe to surface as a recovery artifact.
    """
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return []
    normalized_root = repo_root.resolve()
    receipt_dir = _receipt_dir(normalized_root)
    if not receipt_dir.is_dir():
        return []
    paths: list[Path] = []
    seen: set[Path] = set()
    with file_lock(_receipt_lock_path(receipt_dir, pr)):
        for receipt_path in receipt_dir.glob(f"direct-review-{pr}-*.json"):
            try:
                payload: Any = json.loads(receipt_path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                worktree = _validated_worktree_path(
                    normalized_root, issue, Path(str(payload.get("worktree", "")))
                )
                if (
                    payload.get("schema_version") != _RECEIPT_VERSION
                    or payload.get("reason") != _REMOTE_CHANGED_REASON
                    or payload.get("repo_root") != str(normalized_root)
                    or payload.get("issue") != issue
                    or payload.get("pr") != pr
                    or not isinstance(payload.get("branch"), str)
                    or not _is_full_sha(payload.get("expected_remote_sha"))
                    or not _is_full_sha(payload.get("source_sha"))
                    or not worktree.is_dir()
                    or worktree in seen
                ):
                    continue
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                continue
            seen.add(worktree)
            paths.append(worktree)
    return sorted(paths)
