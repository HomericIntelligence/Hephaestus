"""Own the GitHub merge mutations for one pull request.

The module holds the merge-queue reconciliation and the native auto-merge
enablement. The concern is separate from the label and pull-request
mutations, so each file stays inside its architecture budget.
"""

import re
import subprocess
import time
from threading import Event

from hephaestus.automation.github_api import (
    GraphQLDeterministicError,
    GraphQLMutationOutcomeUnknownError,
    GraphQLResponseError,
    GraphQLRetryableError,
    MergeQueueAlreadyEnqueuedError,
)

from .pipeline.github_jobs import MergeQueueReconciliation
from .pipeline_github_check_policy import EffectiveMergePolicy
from .pipeline_github_comments import PipelineGitHubIssueComments
from .pipeline_github_transport import (
    AutoMergeEnableResult,
    ConditionalMergeResult,
    _parse_included_http_response,
    github_api,
    logger,
)


class PipelineGitHubMergeMutations(PipelineGitHubIssueComments):
    """Own the coordinator-approved merge mutations for one pull request."""

    def reconcile_merge_queue_entry(
        self,
        pr_number: int,
        pull_request_id: str,
        reviewed_sha: str,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> MergeQueueReconciliation:
        """Classify the live queue entry for one exact open pull-request head."""
        if cancellation.is_set():
            return MergeQueueReconciliation.UNAVAILABLE
        timeout = deadline_s - time.monotonic()
        if timeout <= 0:
            return MergeQueueReconciliation.UNAVAILABLE
        owner, name = self._owner_name()
        try:
            pull_request = self._graphql_with_timeout(
                github_api.pull_request_merge_queue_reconciliation_query(owner, name, pr_number),
                timeout,
                number=pr_number,
            )
        except (GraphQLResponseError, RuntimeError, OSError, subprocess.SubprocessError):
            return MergeQueueReconciliation.UNAVAILABLE
        if cancellation.is_set() or time.monotonic() >= deadline_s:
            return MergeQueueReconciliation.UNAVAILABLE
        if (
            pull_request.get("id") != pull_request_id
            or pull_request.get("state") != "OPEN"
            or pull_request.get("headRefOid") != reviewed_sha
        ):
            return MergeQueueReconciliation.UNAVAILABLE
        entry = pull_request.get("mergeQueueEntry")
        if isinstance(entry, dict):
            return MergeQueueReconciliation.PRESENT
        if entry is None:
            return MergeQueueReconciliation.REMOVED
        return MergeQueueReconciliation.UNAVAILABLE

    def _existing_queue_result(
        self,
        pr_number: int,
        pull_request_id: str,
        reviewed_sha: str,
        deadline_s: float,
        cancellation: Event | None,
    ) -> ConditionalMergeResult | None:
        """Read back a successful queue result for the exact open PR head."""
        if cancellation is not None and cancellation.is_set():
            return None
        timeout = deadline_s - time.monotonic()
        if timeout <= 0:
            return None
        owner, name = self._owner_name()
        try:
            pull_request = self._graphql_with_timeout(
                github_api.pull_request_queue_entry_query(owner, name, pr_number),
                timeout,
                number=pr_number,
            )
        except (GraphQLResponseError, RuntimeError, OSError, subprocess.SubprocessError):
            return None
        if cancellation is not None and cancellation.is_set():
            return None
        if time.monotonic() >= deadline_s:
            return None
        entry = pull_request.get("mergeQueueEntry")
        if (
            pull_request.get("id") != pull_request_id
            or pull_request.get("state") != "OPEN"
            or pull_request.get("headRefOid") != reviewed_sha
            or not isinstance(entry, dict)
        ):
            return None
        return ConditionalMergeResult(
            status=200,
            body={"merged": False, "queue_entry_id": entry["id"]},
            queued=True,
        )

    def _enqueue_pr_if_head(
        self,
        pr_number: int,
        pull_request_id: str | None,
        reviewed_sha: str,
        deadline_s: float,
        cancellation: Event | None,
    ) -> ConditionalMergeResult:
        """Request one exact-head queue admission without internal replay."""
        if not isinstance(pull_request_id, str) or not pull_request_id:
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        if cancellation is not None and cancellation.is_set():
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        timeout = deadline_s - time.monotonic()
        if timeout <= 0:
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        try:
            receipt = self._graphql_with_timeout(
                github_api.enqueue_pull_request_mutation(pull_request_id, reviewed_sha),
                timeout,
            )
        except MergeQueueAlreadyEnqueuedError as exc:
            logger.warning("PR #%s: merge-queue admission failed: %s", pr_number, exc)
            reconciled = self._existing_queue_result(
                pr_number,
                pull_request_id,
                reviewed_sha,
                deadline_s,
                cancellation,
            )
            if reconciled is not None:
                return reconciled
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        except GraphQLMutationOutcomeUnknownError as exc:
            logger.warning("PR #%s: merge-queue admission failed: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        except GraphQLRetryableError as exc:
            logger.warning("PR #%s: merge-queue admission failed: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        except (GraphQLDeterministicError, GraphQLResponseError) as exc:
            logger.warning("PR #%s: merge-queue admission failed: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        return ConditionalMergeResult(
            status=200,
            body={"merged": False, "queue_entry_id": receipt["id"]},
            queued=True,
        )

    def merge_pr_if_head(
        self,
        pr_number: int,
        reviewed_sha: str,
        *,
        policy: EffectiveMergePolicy,
        pull_request_id: str | None = None,
        deadline_s: float | None = None,
        cancellation: Event | None = None,
    ) -> ConditionalMergeResult:
        """Request the policy-selected merge route for the reviewed SHA.

        The stage owns review and check admission. This adapter only enforces
        the SHA condition and performs one direct merge or queue-admission
        request. Queue admission is not native auto-merge. A stage-owned
        lifecycle read decides whether an ambiguous request can run again.
        """
        if (
            pr_number <= 0
            or not isinstance(reviewed_sha, str)
            or not re.fullmatch(r"[0-9a-fA-F]{40}", reviewed_sha)
            or not isinstance(policy, EffectiveMergePolicy)
            or (not policy.merge_queue_required and not policy.strict_update_enforced)
        ):
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        owner, name = self._owner_name()
        if self._skip(f"request the policy merge route for PR #{pr_number} at {reviewed_sha}"):
            return ConditionalMergeResult(status=None, body=None, dry_run=True)
        if cancellation is not None and cancellation.is_set():
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        timeout = float(self._gh_timeout)
        operation_deadline_s = time.monotonic() + timeout
        if deadline_s is not None:
            operation_deadline_s = min(operation_deadline_s, deadline_s)
            timeout = operation_deadline_s - time.monotonic()
        if timeout <= 0:
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        if policy.merge_queue_required:
            return self._enqueue_pr_if_head(
                pr_number,
                pull_request_id,
                reviewed_sha,
                operation_deadline_s,
                cancellation,
            )
        try:
            result = self._deadline_gh_call(
                [
                    "api",
                    "--method",
                    "PUT",
                    "--include",
                    f"/repos/{owner}/{name}/pulls/{pr_number}/merge",
                    "-f",
                    f"sha={reviewed_sha}",
                    "-f",
                    "merge_method=squash",
                ],
                check=False,
                retry_on_rate_limit=False,
                max_retries=1,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: conditional merge transport failure: %s", pr_number, exc)
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        status, body, malformed = _parse_included_http_response(result.stdout or "")
        if status is None:
            return ConditionalMergeResult(status=None, body=None, transport_error=True)
        return ConditionalMergeResult(status=status, body=body, malformed=malformed)

    def enable_auto_merge(
        self,
        pr_number: int,
        *,
        pull_request_id: str | None = None,
        deadline_s: float | None = None,
        cancellation: Event | None = None,
    ) -> AutoMergeEnableResult:
        """Enable GitHub native auto-merge for one pull request.

        This is the whole merge contract after the implementation-GO label. The
        caller owns the approval conditions; this adapter owns the single
        mutation. GitHub applies branch protection, the required checks, and any
        merge queue, and rebases on a real conflict on its own.
        """
        if pr_number <= 0:
            return AutoMergeEnableResult(enabled=False, malformed=True)
        if self._skip(f"enable auto-merge for PR #{pr_number}"):
            return AutoMergeEnableResult(enabled=False, dry_run=True)
        if cancellation is not None and cancellation.is_set():
            return AutoMergeEnableResult(enabled=False, transport_error=True)
        timeout = float(self._gh_timeout)
        if deadline_s is not None:
            timeout = min(timeout, deadline_s - time.monotonic())
        if timeout <= 0:
            return AutoMergeEnableResult(enabled=False, transport_error=True)
        try:
            result = self._deadline_gh_call(
                [
                    "pr",
                    "merge",
                    str(pr_number),
                    "--squash",
                    "--auto",
                ],
                check=False,
                retry_on_rate_limit=False,
                max_retries=1,
                timeout=timeout,
            )
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: auto-merge enable failed: %s", pr_number, exc)
            return AutoMergeEnableResult(enabled=False, transport_error=True)
        # `gh` reports success on stdout. It writes informational notes on
        # stderr, so the exit code decides the outcome. It reports an
        # already-enabled or already-queued pull request with a non-zero exit
        # code and a note on stderr.
        lowered = (result.stdout or "").lower()
        if "auto-merge" in lowered and "already" in lowered:
            return AutoMergeEnableResult(enabled=True, already_enabled=True)
        if "auto-merge" in lowered or "enabled" in lowered:
            return AutoMergeEnableResult(enabled=True)
        if result.returncode == 0:
            return AutoMergeEnableResult(enabled=True)
        stderr_lowered = (result.stderr or "").lower()
        if "already queued to merge" in stderr_lowered:
            return AutoMergeEnableResult(enabled=True, already_enabled=True)
        if "auto-merge" in stderr_lowered and "already" in stderr_lowered:
            return AutoMergeEnableResult(enabled=True, already_enabled=True)
        return AutoMergeEnableResult(enabled=False, malformed=True)
