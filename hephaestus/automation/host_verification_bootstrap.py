"""Authenticate the single PR 3006 source-review bootstrap exception."""

from __future__ import annotations

import hashlib
import json
import re
import weakref
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from hephaestus.automation.review_journal import IssueComment

BOOTSTRAP_MARKER = "<!-- hephaestus-host-verification-bootstrap:v1 -->"
BOOTSTRAP_REPOSITORY = "HomericIntelligence/Hephaestus"
BOOTSTRAP_PROOF_KEY = "host_verification_bootstrap_proof"
BOOTSTRAP_MANIFEST: tuple[tuple[str, str], ...] = (
    ("M", "COMPATIBILITY.md"),
    ("M", "docs/architecture.md"),
    ("M", "docs/ci/required-checks.md"),
    ("M", "hephaestus/automation/ci_driver.py"),
    ("M", "hephaestus/automation/implementer.py"),
    ("M", "hephaestus/automation/loop_runner.py"),
    ("M", "hephaestus/automation/pipeline/coordinator.py"),
    ("M", "hephaestus/automation/pipeline/coordinator_types.py"),
    ("A", "hephaestus/automation/pipeline/host_verification_pyxis.py"),
    ("M", "hephaestus/automation/pipeline/stages/pr_review_jobs.py"),
    ("M", "hephaestus/automation/pipeline/stages/pr_review_receipts.py"),
    ("M", "hephaestus/automation/pipeline/worker_pool.py"),
    ("M", "hephaestus/automation/pr_reviewer.py"),
    ("A", "hephaestus/automation/pyxis_artifact_io.py"),
    ("M", "hephaestus/cli/__init__.py"),
    ("M", "hephaestus/cli/utils.py"),
    ("M", "justfile"),
    ("M", "pyproject.toml"),
    ("M", "scripts/README.md"),
    ("A", "scripts/prepare_host_verification_pyxis_image.py"),
    ("M", "tests/conftest.py"),
    ("A", "tests/integration/test_host_verification_pyxis_e2e.py"),
    ("M", "tests/unit/automation/pipeline/test_backpressure.py"),
    ("M", "tests/unit/automation/pipeline/test_coordinator_edges.py"),
    ("A", "tests/unit/automation/pipeline/test_host_verification_pyxis.py"),
    ("M", "tests/unit/automation/pipeline/test_pipeline_flag.py"),
    ("M", "tests/unit/automation/pipeline/test_worker_pool.py"),
    ("M", "tests/unit/automation/test_automation_parsers.py"),
    ("M", "tests/unit/ci/test_pytest_control_options.py"),
    ("A", "tests/unit/scripts/test_prepare_host_verification_pyxis_image.py"),
)
BOOTSTRAP_MANIFEST = tuple(sorted(BOOTSTRAP_MANIFEST))


class BootstrapGrantError(ValueError):
    """The selected grant does not authorize this exact source review."""


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def _sha(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) is not None


def _path(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and not value.startswith("/")
        and "\\" not in value
        and all(ord(char) >= 32 and ord(char) != 127 for char in value)
        and PurePosixPath(value).as_posix() == value
        and all(part not in {".", "..", ".git"} for part in value.split("/"))
    )


def parse_status_manifest(raw: str) -> tuple[tuple[str, str], ...]:
    """Parse exact no-rename Git status records without hiding deletions."""
    if not isinstance(raw, str) or len(raw) > 1_000_000:
        raise BootstrapGrantError("status manifest is invalid")
    if not raw:
        return ()
    records = raw.split("\0")
    if records.pop() != "" or len(records) % 2:
        raise BootstrapGrantError("status manifest is incomplete")
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for status, path in zip(records[::2], records[1::2], strict=True):
        if status not in {"A", "M", "D", "T", "U"} or not path or path in seen:
            raise BootstrapGrantError("status manifest contains an invalid record")
        seen.add(path)
        result.append((status, path))
    return tuple(sorted(result))


def _manifest(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or len(value) != len(BOOTSTRAP_MANIFEST):
        raise BootstrapGrantError("bootstrap manifest does not match the mandatory map")
    result: list[tuple[str, str]] = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"status", "path"}:
            raise BootstrapGrantError("bootstrap manifest record is invalid")
        status, path = row["status"], row["path"]
        if not isinstance(status, str) or not _path(path):
            raise BootstrapGrantError("bootstrap manifest record is invalid")
        result.append((status, path))
    canonical = tuple(sorted(result))
    if canonical != tuple(sorted(BOOTSTRAP_MANIFEST)):
        raise BootstrapGrantError("bootstrap manifest does not match the mandatory map")
    return canonical


def _closed_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise BootstrapGrantError("bootstrap grant contains duplicate keys")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BootstrapProof:
    """Bind an authenticated grant to one process and exact review identity."""

    comment_id: int
    body_sha256: str
    repository: str
    issue: int
    pr: int
    head_sha: str
    base_sha: str
    manifest_sha256: str
    manifest: tuple[tuple[str, str], ...]


_PROOFS: weakref.WeakValueDictionary[int, BootstrapProof] = weakref.WeakValueDictionary()


def is_process_bootstrap_proof(value: object) -> bool:
    """Reject reconstructed or externally supplied proof-shaped values."""
    return isinstance(value, BootstrapProof) and _PROOFS.get(id(value)) is value


def authenticate_bootstrap_grant(
    comments: list[IssueComment],
    *,
    comment_id: int,
    repository: str,
    issue: int,
    pr: int,
    head_sha: str,
    base_sha: str,
    manifest: object,
) -> BootstrapProof:
    """Authenticate the selected operator comment against fresh checkout facts."""
    if (
        type(comment_id) is not int
        or comment_id <= 0
        or repository != BOOTSTRAP_REPOSITORY
        or type(issue) is not int
        or issue != 2701
        or type(pr) is not int
        or pr != 3006
        or not _sha(head_sha)
        or not _sha(base_sha)
        or not isinstance(manifest, tuple)
        or manifest != tuple(sorted(BOOTSTRAP_MANIFEST))
    ):
        raise BootstrapGrantError("bootstrap target or checkout manifest is invalid")
    selected = [comment for comment in comments if comment.database_id == comment_id]
    if len(selected) != 1:
        raise BootstrapGrantError("bootstrap grant is missing or duplicated")
    comment = selected[0]
    if (
        comment.viewer_did_author is not True
        or not comment.author_login
        or comment.author_association not in {"OWNER", "MEMBER", "COLLABORATOR"}
        or comment.author_login.endswith("[bot]")
        or len(comment.body) > 32_768
        or not comment.body.startswith(BOOTSTRAP_MARKER + "\n")
    ):
        raise BootstrapGrantError("bootstrap grant ownership or marker is invalid")
    try:
        body = json.loads(comment.body[len(BOOTSTRAP_MARKER) :], object_pairs_hook=_closed_pairs)
    except (ValueError, RecursionError) as exc:
        raise BootstrapGrantError("bootstrap grant JSON is invalid") from exc
    if not isinstance(body, dict) or set(body) != {
        "repository",
        "issue",
        "pr",
        "head_sha",
        "base_sha",
        "boundary",
        "state",
        "manifest",
    }:
        raise BootstrapGrantError("bootstrap grant schema is invalid")
    if (
        body["repository"] != repository
        or type(body["issue"]) is not int
        or body["issue"] != issue
        or type(body["pr"]) is not int
        or body["pr"] != pr
        or body["head_sha"] != head_sha
        or body["base_sha"] != base_sha
        or body["boundary"] != "linux-pyxis-enroot"
        or body["state"] != "approved"
    ):
        raise BootstrapGrantError("bootstrap grant is revoked or does not match the target")
    canonical = _manifest(body["manifest"])
    proof = BootstrapProof(
        comment_id,
        _digest(body),
        repository,
        issue,
        pr,
        head_sha,
        base_sha,
        _digest(canonical),
        canonical,
    )
    _PROOFS[id(proof)] = proof
    return proof


def revalidate_bootstrap_proof(proof: object, comments: list[IssueComment]) -> bool:
    """Require a fresh unchanged grant for the same process-local proof."""
    if not isinstance(proof, BootstrapProof) or not is_process_bootstrap_proof(proof):
        return False
    try:
        fresh = authenticate_bootstrap_grant(
            comments,
            comment_id=proof.comment_id,
            repository=proof.repository,
            issue=proof.issue,
            pr=proof.pr,
            head_sha=proof.head_sha,
            base_sha=proof.base_sha,
            manifest=proof.manifest,
        )
    except (BootstrapGrantError, TypeError, AttributeError):
        return False
    return fresh == proof


def read_fresh_bootstrap_proof(proof: object, github: Any) -> bool:
    """Read the authenticated journal again in its exact repository scope."""
    if not isinstance(proof, BootstrapProof) or not is_process_bootstrap_proof(proof):
        return False
    if getattr(github, "_repo_slug", None) != proof.repository:
        return False
    try:
        return revalidate_bootstrap_proof(proof, github.issue_comments(proof.pr))
    except Exception:
        return False


def revoke_bootstrap_go(github: Any, *, pr: int, head_sha: str) -> bool:
    """Replace stale GO only on an exact open, unarmed PR, then verify NOGO."""
    try:
        state = github.gh_pr_state(pr)
        if not isinstance(state, dict) or (
            state.get("state") != "OPEN"
            or "autoMergeRequest" not in state
            or state["autoMergeRequest"] is not None
            or state.get("headRefOid") != head_sha
            or not isinstance(state.get("id"), str)
            or not state["id"]
        ):
            return False
        github.mark_pr_implementation_no_go(pr)
        current = github.gh_pr_state(pr)
        if not isinstance(current, dict) or (
            current.get("state") != "OPEN"
            or "autoMergeRequest" not in current
            or current["autoMergeRequest"] is not None
            or current.get("headRefOid") != head_sha
            or current.get("id") != state["id"]
        ):
            return False
        has_go, has_no_go = github.pr_has_implementation_state_label(pr)
        return has_go is False and has_no_go is True
    except Exception:
        return False
