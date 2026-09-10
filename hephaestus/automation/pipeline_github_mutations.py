# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
import subprocess
import time
from threading import Event

import hephaestus.automation.git_runtime as git_runtime
from hephaestus.automation.github_api import (
    GraphQLDeterministicError,
    GraphQLMutationOutcomeUnknownError,
    GraphQLResponseError,
    GraphQLRetryableError,
    MergeQueueAlreadyEnqueuedError,
    prs as github_prs,
)
from hephaestus.automation.operation_deadlines import operation_deadline_after

from .pipeline_github_check_policy import EffectiveMergePolicy
from .pipeline_github_comments import PipelineGitHubIssueComments
from .pipeline_github_transport import *


class PipelineGitHubMutations(PipelineGitHubIssueComments):
    """Own coordinator-approved non-review GitHub mutations."""

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

    def add_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably add labels (``gh_issue_add_labels``)."""
        if self._skip(f"add labels {labels} to #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._add_labels(issue_number, labels)
            return
        github_api.gh_issue_add_labels(issue_number, labels)

    def remove_labels(self, issue_number: int, labels: list[str]) -> None:
        """Durably remove labels (``gh_issue_remove_labels``)."""
        if self._skip(f"remove labels {labels} from #{issue_number}"):
            return
        if self._repo_slug is not None:
            self._remove_labels(issue_number, labels)
            return
        github_api.gh_issue_remove_labels(issue_number, labels)

    def edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Atomically add+remove labels in a single ``gh issue edit``."""
        try:
            self._edit_labels(issue_number, add=add, remove=remove)
        except (subprocess.SubprocessError, OSError, RuntimeError) as exc:
            raise RuntimeError(f"failed to edit labels on issue #{issue_number}: {exc}") from exc

    def _edit_labels(self, issue_number: int, *, add: list[str], remove: list[str]) -> None:
        """Execute the label mutation after the public error boundary."""
        if self._skip(f"edit labels on #{issue_number} (+{add} -{remove})"):
            return
        if self._repo_slug is not None:
            if add:
                existing = self._label_names()
                for label in add:
                    if label not in existing:
                        self._create_label(label)
                        existing.add(label)
        elif add:
            github_api._ensure_labels_exist(add)
        cmd = ["issue", "edit", str(issue_number)]
        for label in add:
            cmd.extend(["--add-label", label])
        for label in remove:
            cmd.extend(["--remove-label", label])
        if add or remove:
            (self._gh if self._repo_slug is not None else gh_call)(cmd)

    def close_issue_as_covered(self, issue_number: int, pr_number: int) -> None:
        """Close the issue as covered by a merged PR (``_review_utils``)."""
        if self._skip(f"close #{issue_number} as covered by PR #{pr_number}"):
            return
        if self._repo_slug is not None:
            self._gh(
                [
                    "issue",
                    "close",
                    str(issue_number),
                    "--comment",
                    f"Closed by merged PR #{pr_number} (Closes #{issue_number}).",
                ],
                check=False,
            )
            return
        close_issue_as_covered(issue_number, pr_number)

    def create_pr(
        self,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        *,
        strict_absence: bool = False,
    ) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        PR creation requires an explicit repository identity.

        First select and reuse an open PR on the supplied branch, then use
        ``find_pr_for_issue`` as the issue-level fallback before creating a
        PR with the *given* title/body — NOT ``pr_manager.ensure_pr_created``,
        which would discard the stage's composed body (protocol docstring).
        Dry-run returns 0 (no PR).
        """
        if self._repo_slug is None:
            raise RuntimeError("create PR requires a repo-scoped PipelineGitHub accessor")
        if type(strict_absence) is not bool:
            raise ValueError("strict_absence must be a boolean")
        if strict_absence:
            self._require_strict_pr_absence(issue_number, branch)
        else:
            open_prs = self._open_prs_for_branch(branch)
            existing_on_branch = github_api._select_open_pr_for_base(open_prs, "main")
            if existing_on_branch is not None:
                return existing_on_branch
            existing = self.find_pr_for_issue(issue_number)
            if existing:
                return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        try:
            return self._create_pr_once(issue_number, branch, title, body, strict_absence)
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError, TypeError):
            if not strict_absence:
                raise
            # A failed create can still have reached GitHub. Never adopt its result.
            self._require_strict_pr_absence(issue_number, branch)
            raise RuntimeError("strict PR creation outcome is ambiguous") from None

    def _require_strict_pr_absence(self, issue_number: int, branch: str) -> None:
        """Reject reuse using a fresh complete branch and issue read."""
        try:
            branches = self.open_prs_for_branch(branch)
            issue_pr = self.find_pr_for_issue(issue_number)
        except (subprocess.SubprocessError, OSError, RuntimeError, ValueError, TypeError):
            raise RuntimeError("strict PR absence read failed") from None
        if branches or issue_pr is not None:
            raise RuntimeError("strict PR absence violated by an existing open PR")

    def _create_pr_once(
        self,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        strict_absence: bool,
    ) -> int:
        """Submit one create request without a PR adoption path."""
        deadline = self._operation_deadline_s or operation_deadline_after(self._gh_timeout)
        with self.operation_deadline(deadline), git_runtime.operation_deadline(deadline):
            repository = self._owner_name()
            github_api._assert_body_has_closes(body)
            github_prs._assert_branch_commits_signed(
                branch,
                base="main",
                run_git=self._run_signature_git,
                verify_commit=lambda oid: github_prs._gh_commit_is_verified(
                    oid, repository=repository, run_gh=self._deadline_gh_call
                ),
            )
            with github_api._body_file(body) as body_path:
                result = self._gh(
                    [
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--base",
                        "main",
                        "--title",
                        github_api.strip_null_bytes(title),
                        "--body-file",
                        body_path,
                    ]
                )
        raw_output = result.stdout
        if strict_absence and (
            not isinstance(raw_output, str) or getattr(result, "returncode", 0) != 0
        ):
            raise RuntimeError("strict PR creation outcome is ambiguous")
        output = raw_output.strip()
        match = re.search(r"/pull/(\d+)", output)
        if match:
            return int(match.group(1))
        if strict_absence:
            raise RuntimeError("strict PR creation outcome is ambiguous")
        logger.error("Failed to parse PR number from gh pr create output: %r", raw_output)
        raise RuntimeError(f"Failed to parse PR number from gh pr create output: {raw_output!r}")

    def _run_signature_git(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """Run one signature check in the repository under the operation bounds."""
        return git_runtime.run(
            argv,
            cwd=self._repo_root,
            check=False,
            timeout=self._operation_timeout(self._gh_timeout),
            shutdown=self._operation_shutdown,
        )

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-no-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        self._add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        self._remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if has_go or not has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-no-go label read-back failed")

    def ensure_state_labels(self) -> None:
        """Ensure the ``state:*`` label vocabulary exists on the repo.

        Repo-stage step 1 [M] (doc section 1): idempotent
        ``_ensure_labels_exist`` over the full ``state_labels`` vocabulary.
        """
        # The shared vocabulary includes the orthogonal issue-work guard.
        wanted = list(STATE_LABEL_SPECS)
        if self._skip(f"ensure state labels exist: {wanted}"):
            return
        if self._repo_slug is not None:
            existing = self._label_names()
            for label in wanted:
                if label not in existing:
                    self._create_label(label)
            return
        github_api._ensure_labels_exist(wanted)
