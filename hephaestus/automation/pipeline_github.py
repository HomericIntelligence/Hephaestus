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
        _parse_included_http_response as _parse_included_http_response,
        _rate_budget_ok_impl,
        _with_severity_marker as _with_severity_marker,
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
        re,
        scope_retraction_marker,
        subprocess,
        sys,
        time,
    )
else:
    from .pipeline_github_transport import *
from hephaestus.automation.github_api import gh_call
from hephaestus.utils.file_lock import file_lock

from ._review_utils import (
    close_issue_as_covered,
    find_merged_closing_pr,
    find_merged_pr_for_issue,
    get_pr_head_branch,
)
from .git_utils import issue_auto_impl_branch_name
from .pipeline_github_audit import PipelineGitHubAuditReceipts
from .pipeline_github_check_policy import PipelineGitHubCheckPolicy
from .pipeline_github_mutations import PipelineGitHubMutations
from .pipeline_github_queries import PipelineGitHubQueries
from .pipeline_github_required_checks import PipelineGitHubRequiredChecks
from .pipeline_github_review_queries import PipelineGitHubReviewQueries
from .pipeline_github_reviews import PipelineGitHubReviews
from .pipeline_github_scope_expansion import PipelineGitHubScopeExpansion

_CLOSES_ISSUE_LINE_RE = re.compile(r"^Closes #(\d+)\s*$", re.MULTILINE)
_IMPLEMENTATION_REPLY_BODY_RE = re.compile(
    r"(?s)\A(.*)\n\n<!-- hephaestus-implementation-reply:[0-9a-f]{24} -->\n"
    r"<!-- hephaestus-implementation-batch:([0-9a-f]{32}) -->\Z"
)
_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


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


class PipelineGitHub(
    PipelineGitHubTransport,
    PipelineGitHubQueries,
    PipelineGitHubCheckPolicy,
    PipelineGitHubRequiredChecks,
    PipelineGitHubReviewQueries,
    PipelineGitHubReviews,
    PipelineGitHubAuditReceipts,
    PipelineGitHubMutations,
    PipelineGitHubScopeExpansion,
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
