"""Coordinator-owned GitHub adapter and its compatibility façade."""

from __future__ import annotations

import typing as _typing

# ruff: noqa: F401, F403, I001
# The transport import must precede these explicit compatibility seams: its
# star export contains proxy aliases that the façade then replaces with the
# patchable originals.
if _typing.TYPE_CHECKING:
    from .pipeline_github_transport import (
        ALL_IMPLEMENTATION_STATE_LABELS,
        ALL_STATE_LABELS,
        PLAN_CANONICAL_MARKER,
        PLAN_COMMENT_MARKER,
        PLAN_REVIEW_CANONICAL_MARKER,
        PLAN_REVIEW_PREFIX,
        SCOPE_RETRACTION_MARKER_PREFIX,
        SEVERITY_MARKER_PREFIX,
        SKIP_REASON_MARKER,
        STATE_IMPLEMENTATION_GO,
        STATE_IMPLEMENTATION_NO_GO,
        STATE_LABEL_SPECS,
        STATE_SKIP,
        VALID_SEVERITIES,
        Any,
        ArmingStateStore,
        ConditionalMergeResult,
        ImplementationThreadReplyResult,
        IssueComment,
        LockUnavailableError,
        Path,
        PipelineGitHubTransport,
        ReviewerThreadReconciliationResult,
        _CLOSES_ISSUE_LINE_RE,
        _FULL_COMMIT_SHA_RE,
        _HTTP_STATUS_RE,
        _IMPLEMENTATION_REPLY_BODY_RE,
        _STANDALONE_VERDICT_LINE_RE,
        _CompatCallable,
        _compat,
        _rate_budget_ok_impl,
        annotations,
        blocked_audit_recovery_body,
        close_issue_as_covered,
        ensure_state_dir,
        file_lock,
        find_merged_closing_pr,
        find_merged_pr_for_issue,
        format_skip_reason_comment,
        get_pr_head_branch,
        gh_call,
        github_api,
        has_exact_closing_line,
        has_label,
        hashlib,
        is_implementation_go,
        issue_auto_impl_branch_name,
        json,
        logger,
        logging,
        normalize_scope_retraction_paths,
        quote,
        re,
        scope_retraction_marker,
        subprocess,
        sys,
        time,
    )
else:
    from .pipeline_github_transport import *
from hephaestus.github.client import gh_call
from hephaestus.utils.file_lock import file_lock

from ._review_utils import (
    close_issue_as_covered,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    get_pr_head_branch,
)
from .git_utils import issue_auto_impl_branch_name
from .pipeline_github_audit import PipelineGitHubAuditReceipts
from .pipeline_github_authorization import PipelineGitHubAuthorization
from .pipeline_github_mutations import PipelineGitHubMutations
from .pipeline_github_queries import PipelineGitHubQueries
from .pipeline_github_reviews import PipelineGitHubReviews

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
    """Parse the final status and JSON object from ``gh api --include`` output.

    A missing HTTP status means the CLI did not provide enough evidence to
    classify the request. A received non-object or invalid body is explicitly
    malformed so callers fail closed rather than treating it as transport
    ambiguity.
    """
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


def rate_limit_remaining(*, timeout: int | None = None) -> tuple[int, int] | None:
    """Return ``(remaining, reset_epoch)`` for the GraphQL budget, or ``None``.

    Feeds the coordinator's non-blocking rate gate. A blocking *sleeping* guard
    would be fatal for a single coordinator thread, so the pipeline timer-parks
    instead (see ``coordinator._rate_budget_ok``). ``timeout`` bounds the live
    GitHub CLI probe at the calling CLI boundary.
    """
    try:
        out = gh_call(["api", "rate_limit"], timeout=timeout)
    except (subprocess.SubprocessError, RuntimeError, OSError):
        return None
    try:
        data = json.loads(out.stdout)
        gql = data["resources"]["graphql"]
        return int(gql["remaining"]), int(gql["reset"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def rate_budget_ok(
    now_epoch: float | None = None,
    *,
    enabled: bool = True,
    threshold: int = 200,
    timeout: int | None = None,
) -> tuple[bool, float]:
    """Non-blocking GraphQL rate-budget gate for the coordinator.

    Args:
        now_epoch: Current epoch seconds (injectable for tests).
        timeout: Maximum seconds for the live GitHub CLI budget probe.

    Returns:
        ``(ok, park_delay_s)``. ``ok`` is False when the GraphQL budget is
        below the explicit threshold (default 200) and the guard is enabled;
        ``park_delay_s`` is the
        seconds until the upstream reset (+5s slack, mirroring the legacy
        guard), 0.0 when ``ok``.

    """
    if not enabled:
        return True, 0.0
    rl = rate_limit_remaining(timeout=timeout)
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
    request requirements under ``required_pull_request_reviews``. A missing
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


class PipelineGitHub(
    PipelineGitHubTransport,
    PipelineGitHubQueries,
    PipelineGitHubAuthorization,
    PipelineGitHubReviews,
    PipelineGitHubAuditReceipts,
    PipelineGitHubMutations,
):
    """Stable GitHub façade with explicit single-owner semantics.

    Coordinator contexts may cache an instance on the coordinator thread.
    Worker jobs construct a separate instance for each request; sharing an
    instance between threads is unsupported.
    """

    def mark_pr_implementation_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-go"):
            return
        self._add_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        self._remove_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if not has_go or has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-go label read-back failed")
