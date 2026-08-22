"""Coordinator-owned GitHub adapter with live reads and guarded mutations."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote

import hephaestus.automation.github_api as github_api
from hephaestus.automation._review_utils import (
    close_issue_as_covered,
    ensure_state_dir,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    get_pr_head_branch,
    has_exact_closing_line,
)
from hephaestus.automation.arming_state import ArmingStateStore
from hephaestus.automation.git_utils import issue_auto_impl_branch_name
from hephaestus.automation.pipeline.scope_retraction import (
    SCOPE_RETRACTION_MARKER_PREFIX,
    normalize_scope_retraction_paths,
    scope_retraction_marker,
)
from hephaestus.automation.pipeline.stages.base import (
    ConditionalMergeResult,
    ImplementationThreadReplyResult,
    ReviewerThreadReconciliationResult,
)
from hephaestus.automation.prompts.pr_review import (
    SEVERITY_MARKER_PREFIX,
    VALID_SEVERITIES,
)
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    blocked_audit_recovery_body,
)
from hephaestus.automation.state_labels import (
    ALL_IMPLEMENTATION_STATE_LABELS,
    ALL_STATE_LABELS,
    SKIP_REASON_MARKER,
    STATE_IMPLEMENTATION_GO,
    STATE_IMPLEMENTATION_NO_GO,
    STATE_LABEL_SPECS,
    STATE_SKIP,
    format_skip_reason_comment,
    has_label,
    is_implementation_go,
)
from hephaestus.constants import read_timeout_env
from hephaestus.github.client import gh_call
from hephaestus.utils.file_lock import LockUnavailableError, file_lock

from .pipeline_github_contract import _PipelineGitHubHost

# ruff: noqa: F811
logger = logging.getLogger(__name__)

_CLOSES_ISSUE_LINE_RE = re.compile(r"^Closes #(\d+)\s*$", re.MULTILINE)
_STANDALONE_VERDICT_LINE_RE = re.compile(r"(?i)^\s*verdict\s*:")
_HTTP_STATUS_RE = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.MULTILINE)
_IMPLEMENTATION_REPLY_BODY_RE = re.compile(
    r"(?s)\A(.*)\n\n<!-- hephaestus-implementation-reply:[0-9a-f]{24} -->\n"
    r"<!-- hephaestus-implementation-batch:([0-9a-f]{32}) -->\Z"
)
_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


def _parse_included_http_response(
    stdout: str,
) -> tuple[int | None, dict[str, Any] | None, bool]:
    """Parse ``gh api --include`` status/JSON and fail closed on malformed payloads."""
    matches = list(_HTTP_STATUS_RE.finditer(stdout))
    if not matches:
        return None, None, False
    status = int(matches[-1].group(1))
    headers_end = re.search(r"\r?\n\r?\n", stdout[matches[-1].start() :])
    if headers_end is None:
        return status, None, True
    body_start = matches[-1].start() + headers_end.end()
    body = stdout[body_start:].strip()
    if not body:
        return status, None, True
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return status, None, True
    return (status, parsed, False) if isinstance(parsed, dict) else (status, None, True)


def rate_limit_remaining() -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or ``None``.

    Feeds the coordinator's non-blocking rate gate. A blocking *sleeping* guard
    would be fatal for a single coordinator thread, so the pipeline timer-parks
    instead (see ``coordinator._rate_budget_ok``).
    """
    try:
        out = gh_call(["api", "rate_limit"])
    except (subprocess.SubprocessError, RuntimeError, OSError):
        return None
    try:
        data = json.loads(out.stdout)
        gql = data["resources"]["graphql"]
        return int(gql["remaining"]), int(gql["reset"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def _rate_budget_ok_impl(now_epoch: float | None = None) -> tuple[bool, float]:
    """Non-blocking GraphQL rate-budget gate for the coordinator.

    Args:
        now_epoch: Current epoch seconds (injectable for tests).

    Returns:
        ``(ok, park_delay_s)``. ``ok`` is False when the GraphQL budget is
        below ``HEPHAESTUS_RATE_GUARD_THRESHOLD`` (default 200) and the
        ``HEPHAESTUS_RATE_GUARD`` env gate is enabled; ``park_delay_s`` is the
        seconds until the upstream reset (+5s slack, mirroring the legacy
        guard), 0.0 when ``ok``.

    """
    if os.environ.get("HEPHAESTUS_RATE_GUARD", "1") == "0":
        return True, 0.0
    threshold = read_timeout_env("HEPHAESTUS_RATE_GUARD_THRESHOLD", 200)
    rl = rate_limit_remaining()
    if rl is None:
        return True, 0.0
    remaining, reset_epoch = rl
    if remaining >= threshold:
        return True, 0.0
    now = time.time() if now_epoch is None else now_epoch
    return False, max(0.0, reset_epoch - now + 5.0)


def _with_severity_marker(comment: dict[str, Any]) -> str:
    """Publish a visible reviewer prefix and durable severity marker (#1856).

    An absent/unknown severity is written as ``major`` (blocking) so an
    unclassifiable thread never silently unblocks a GO, and so the pre-#1856
    all-blocking behavior is reproduced until the reviewer's severity is seeded.
    """
    sev = str(comment.get("severity") or "").strip().lower()
    if sev not in VALID_SEVERITIES:
        sev = "major"
    body = str(comment.get("body") or "")
    body = "\n".join(
        line
        for line in body.splitlines()
        if not line.strip().startswith(SEVERITY_MARKER_PREFIX)
        and not line.strip().startswith(SCOPE_RETRACTION_MARKER_PREFIX)
        and not _STANDALONE_VERDICT_LINE_RE.match(line)
    )
    if body.startswith("[Review] "):
        body = body.removeprefix("[Review] ")
    paths = normalize_scope_retraction_paths(comment.get("scope_retraction_paths"))
    markers = [f"{SEVERITY_MARKER_PREFIX} {sev} -->"]
    if paths:
        markers.append(scope_retraction_marker(paths))
    return "\n".join([f"[Review] {body}", *markers])


def _has_no_explicit_pull_request_bypasses(protection: dict[str, Any]) -> bool:
    """Return whether the classic protection response grants no PR bypasses.

    Classic branch protection exposes actor allowances that may bypass pull
    request requirements under ``required_pull_request_reviews``.  A missing
    review-requirement object means this response exposes no such allowance.
    GitHub also omits the allowance field itself when no PR bypass is
    configured. Once the field is present, every allowance collection must be
    present, list-typed, and empty. This is deliberately fail-closed because a
    merge actor must not infer that a malformed allowance is safe.
    """
    reviews = protection.get("required_pull_request_reviews")
    if reviews is None:
        return True
    if not isinstance(reviews, dict):
        return False
    if "bypass_pull_request_allowances" not in reviews:
        return True
    allowances = reviews["bypass_pull_request_allowances"]
    if not isinstance(allowances, dict):
        return False
    for actor_type in ("users", "teams", "apps"):
        actors = allowances.get(actor_type)
        if not isinstance(actors, list) or actors:
            return False
    return True


def _compat(name: str) -> Any:
    facade = sys.modules.get("hephaestus.automation.pipeline_github")
    return getattr(
        facade or __import__("hephaestus.automation.pipeline_github", fromlist=["*"]), name
    )


class _CompatCallable:
    def __init__(self, name: str) -> None:
        self.name = name

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _compat(self.name)(*args, **kwargs)


gh_call = _CompatCallable("gh_call")
close_issue_as_covered = _CompatCallable("close_issue_as_covered")
find_merged_closing_pr = _CompatCallable("find_merged_closing_pr")
find_merged_pr_for_issue = _CompatCallable("find_merged_pr_for_issue")
get_pr_head_branch = _CompatCallable("get_pr_head_branch")
issue_auto_impl_branch_name = _CompatCallable("issue_auto_impl_branch_name")
file_lock = _CompatCallable("file_lock")
# Keep the coordinator's historical patch seam on the façade while allowing
# the runtime collaborator to depend only on this transport module.
rate_budget_ok = cast(Callable[[], tuple[bool, float]], _CompatCallable("rate_budget_ok"))


class PipelineGitHubTransport(_PipelineGitHubHost):
    """Provide repository-scoped GitHub reads and guarded mutations."""

    def __init__(
        self,
        org: str,
        *,
        repo: str | None = None,
        dry_run: bool = False,
        repo_root: Path | None = None,
    ) -> None:
        """Initialize the accessor.

        Args:
            org: GitHub organization.
            repo: Repository name for repository-scoped pipeline work. The
                org-only form is retained solely for discovery setup; review
                thread reads and all mutations require a concrete repository.
            dry_run: When True, every mutator logs-and-skips.
            repo_root: Repo checkout root anchoring the drive-green arming
                state dir (defaults to the current working directory).

        """
        self.org = org
        self.repo = repo
        self.dry_run = dry_run
        self._repo_root = repo_root or Path.cwd()
        self._arming = ArmingStateStore(lambda: ensure_state_dir(self._repo_root))
        self._viewer_login_cache: str | None = None

    @property
    def _repo_slug(self) -> str | None:
        if not self.repo:
            return None
        return f"{self.org}/{self.repo}"

    def _owner_name(self) -> tuple[str, str]:
        """Return explicit owner/name for repo-scoped GitHub API calls."""
        if self.repo is None:
            raise RuntimeError("repo-scoped GitHub operation requires a repo")
        return self.org, self.repo

    def _viewer_login(self) -> str:
        """Return the authenticated actor used to own mutable journal comments."""
        if self._viewer_login_cache is None:
            self._viewer_login_cache = github_api.gh_current_login() or ""
        if not self._viewer_login_cache:
            raise RuntimeError("cannot verify GitHub comment ownership: viewer login unavailable")
        return self._viewer_login_cache

    def _comment_owned_by_viewer(self, comment: dict[str, Any]) -> bool:
        """Fail closed unless GitHub proves the current actor authored a comment."""
        if "viewerDidAuthor" in comment:
            return bool(comment.get("viewerDidAuthor"))
        user = comment.get("user") or comment.get("author")
        login = user.get("login") if isinstance(user, dict) else ""
        return bool(login) and str(login).lower() == self._viewer_login().lower()

    def _graphql(self, query: str, **fields: int | str) -> dict[str, Any]:
        """Run a repo-scoped GraphQL query with explicit owner/repo fields."""
        owner, name = self._owner_name()
        argv = ["api", "graphql", "-f", f"query={query}"]
        for key, value in {"owner": owner, "name": name, **fields}.items():
            argv.extend(["-F" if isinstance(value, int) else "-f", f"{key}={value}"])
        try:
            data = json.loads(gh_call(argv).stdout or "{}")
        except (subprocess.SubprocessError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RuntimeError(f"repo-scoped pipeline GraphQL request failed: {exc}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("GraphQL response was not an object")
        github_api._check_graphql_errors(data, "repo-scoped pipeline GraphQL")
        return data

    def _with_repo(self, argv: list[str]) -> list[str]:
        """Append an explicit repo selector when this accessor is repo-scoped."""
        if self._repo_slug is None:
            return argv
        return [*argv, "--repo", self._repo_slug]

    def _gh(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return gh_call(self._with_repo(argv), **kwargs)

    def _label_names(self) -> set[str]:
        if self._repo_slug is None:
            # Org-scoped fallback: always re-fetch so a multithreaded
            # coordinator never trusts another repo's slug-keyed entry (#1858).
            return github_api.gh_list_labels(refresh=True)
        result = self._gh(["label", "list", "--json", "name", "--limit", "200"])
        data = json.loads(result.stdout or "[]")
        return {str(item["name"]) for item in data if isinstance(item, dict) and item.get("name")}

    def _create_label(self, name: str) -> None:
        spec = STATE_LABEL_SPECS.get(name, {})
        cmd = ["label", "create", name, "--color", spec.get("color", "ededed"), "--force"]
        if desc := spec.get("description", ""):
            cmd.extend(["--description", desc])
        self._gh(cmd)

    def _add_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        for label in labels:
            if label not in existing:
                self._create_label(label)
                existing.add(label)
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels:
            cmd.extend(["--add-label", label])
        self._gh(cmd)

    def _remove_labels(self, issue_number: int, labels: list[str]) -> None:
        if not labels:
            return
        existing = self._label_names()
        labels_to_remove = [label for label in labels if label in existing]
        if not labels_to_remove:
            return
        cmd = ["issue", "edit", str(issue_number)]
        for label in labels_to_remove:
            cmd.extend(["--remove-label", label])
        self._gh(cmd)

    @staticmethod
    def _label_names_from_payload(payload: dict[str, Any]) -> list[str]:
        labels = payload.get("labels")
        if not isinstance(labels, list):
            return []
        names: list[str] = []
        for label in labels:
            if isinstance(label, str):
                names.append(label)
            elif isinstance(label, dict) and isinstance(label.get("name"), str):
                names.append(str(label["name"]))
        return names

    def _skip(self, what: str) -> bool:
        """Return True (and log) when dry-run should skip a mutation."""
        if self.dry_run:
            logger.info("[dry-run] would %s", what)
            return True
        return False


# Include private internal APIs used by the service mixins.
# fmt: off
__all__ = [
    'ALL_IMPLEMENTATION_STATE_LABELS', 'ALL_STATE_LABELS', 'PLAN_CANONICAL_MARKER',
    'PLAN_COMMENT_MARKER', 'PLAN_REVIEW_CANONICAL_MARKER', 'PLAN_REVIEW_PREFIX',
    'SCOPE_RETRACTION_MARKER_PREFIX', 'SEVERITY_MARKER_PREFIX', 'SKIP_REASON_MARKER',
    'STATE_IMPLEMENTATION_GO', 'STATE_IMPLEMENTATION_NO_GO', 'STATE_LABEL_SPECS', 'STATE_SKIP',
    'VALID_SEVERITIES', '_CLOSES_ISSUE_LINE_RE', '_FULL_COMMIT_SHA_RE', '_HTTP_STATUS_RE',
    '_IMPLEMENTATION_REPLY_BODY_RE', '_STANDALONE_VERDICT_LINE_RE', 'Any', 'ArmingStateStore',
    'ConditionalMergeResult', 'ImplementationThreadReplyResult', 'IssueComment',
    'LockUnavailableError', 'Path', 'PipelineGitHubTransport',
    'ReviewerThreadReconciliationResult', '_CompatCallable', '_compat',
    '_has_no_explicit_pull_request_bypasses', '_parse_included_http_response',
    '_rate_budget_ok_impl', '_with_severity_marker', 'annotations', 'blocked_audit_recovery_body',
    'close_issue_as_covered', 'ensure_state_dir', 'file_lock', 'find_merged_closing_pr',
    'find_merged_pr_for_issue', 'format_skip_reason_comment', 'get_pr_head_branch', 'gh_call',
    'github_api', 'has_exact_closing_line', 'has_label', 'hashlib', 'is_implementation_go',
    'issue_auto_impl_branch_name', 'json', 'logger', 'logging', 'normalize_scope_retraction_paths',
    'os', 'quote', 'rate_budget_ok', 'rate_limit_remaining', 're',
    'read_timeout_env', 'scope_retraction_marker', 'subprocess', 'sys', 'time']
# fmt: on
