# This mixin consumes the adapter transport namespace by design.
# ruff: noqa: F403, F405
import subprocess
import time
from threading import Event

from hephaestus.automation.github_api import (
    GraphQLDeterministicError,
    GraphQLMutationOutcomeUnknownError,
    GraphQLResponseError,
    GraphQLRetryableError,
)

from .pipeline_github_check_policy import EffectiveMergePolicy
from .pipeline_github_comments import PipelineGitHubIssueComments
from .pipeline_github_transport import *

_ALREADY_QUEUED_MESSAGE = "pull request is already in the queue"
_ALREADY_QUEUED_TRANSPORT = f"unprocessable: {_ALREADY_QUEUED_MESSAGE}"


def _is_already_queued_error(error: GraphQLMutationOutcomeUnknownError) -> bool:
    """Return whether GitHub returned the exact already-queued error."""
    message = str(error).strip().casefold()
    return message == _ALREADY_QUEUED_TRANSPORT or (
        message == _ALREADY_QUEUED_MESSAGE and error.graphql_error_type == "UNPROCESSABLE"
    )


class PipelineGitHubMutations(PipelineGitHubIssueComments):
    """Own coordinator-approved non-review GitHub mutations."""

    def _existing_queue_result(
        self,
        pr_number: int,
        pull_request_id: str,
        reviewed_sha: str,
        *,
        deadline_s: float | None,
        cancellation: Event | None,
    ) -> ConditionalMergeResult | None:
        """Read back a successful queue result for the exact open PR head."""
        if cancellation is not None and cancellation.is_set():
            return None
        timeout = float(self._gh_timeout)
        if deadline_s is not None:
            timeout = min(timeout, deadline_s - time.monotonic())
            if timeout <= 0:
                return None
        owner, name = self._owner_name()
        try:
            pull_request = self._graphql(
                github_api.pull_request_queue_entry_query(owner, name, pr_number),
                timeout=timeout,
                number=pr_number,
            )
        except (GraphQLResponseError, RuntimeError, OSError, subprocess.SubprocessError):
            return None
        if cancellation is not None and cancellation.is_set():
            return None
        if deadline_s is not None and time.monotonic() >= deadline_s:
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
        timeout: float,
        *,
        deadline_s: float | None,
        cancellation: Event | None,
    ) -> ConditionalMergeResult:
        """Request one exact-head queue admission without internal replay."""
        if not isinstance(pull_request_id, str) or not pull_request_id:
            return ConditionalMergeResult(status=None, body=None, malformed=True)
        try:
            receipt = self._graphql_with_timeout(
                github_api.enqueue_pull_request_mutation(pull_request_id, reviewed_sha),
                timeout,
            )
        except GraphQLMutationOutcomeUnknownError as exc:
            logger.warning("PR #%s: merge-queue admission failed: %s", pr_number, exc)
            if _is_already_queued_error(exc):
                reconciled = self._existing_queue_result(
                    pr_number,
                    pull_request_id,
                    reviewed_sha,
                    deadline_s=deadline_s,
                    cancellation=cancellation,
                )
                if reconciled is not None:
                    return reconciled
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
        if deadline_s is not None:
            timeout = min(timeout, deadline_s - time.monotonic())
            if timeout <= 0:
                return ConditionalMergeResult(status=None, body=None, transport_error=True)
        if policy.merge_queue_required:
            return self._enqueue_pr_if_head(
                pr_number,
                pull_request_id,
                reviewed_sha,
                timeout,
                deadline_s=deadline_s,
                cancellation=cancellation,
            )
        try:
            result = gh_call(
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

    def drive_green_learn_terminal(self, issue_number: int) -> bool:
        """Return True when the post-merge ``/learn`` is already terminal.

        Mirrors ``ci_driver.CIDriver._learn_record_terminal`` over the issue's
        arming record: captured/succeeded timestamps or a terminal
        ``learn_status`` mean ``/learn`` must never fire again (#848).
        """
        record = self._arming.load(issue_number) or {}
        if record.get("learn_captured_at") or record.get("learn_succeeded_at"):
            return True
        return str(record.get("learn_status") or "").lower() in {"succeeded", "failed"}

    def drive_green_learn_inflight(self, issue_number: int) -> bool:
        """Return whether a persisted /learn dispatch may already have run.

        A process can fail after the agent receives its prompt but before it
        writes its outcome. This durable claim is intentionally not treated as
        a successful result: recovery retains the record for inspection, but
        must never repeat the external learning side effect.
        """
        record = self._arming.load(issue_number) or {}
        return str(record.get("learn_status") or "").lower() == "in_progress"

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

    def create_pr(self, issue_number: int, branch: str, title: str, body: str) -> int:
        """Durably ensure the PR exists and return its number (idempotent).

        PR creation requires a repo-scoped accessor.  The legacy helper can
        alter auto-merge state, so an unscoped caller must fail closed rather
        than delegate to it.

        First select and reuse an open PR on the supplied branch, then use
        ``find_pr_for_issue`` as the issue-level fallback before creating a
        PR with the *given* title/body — NOT ``pr_manager.ensure_pr_created``,
        which would discard the stage's composed body (protocol docstring).
        Dry-run returns 0 (no PR).
        """
        if self._repo_slug is None:
            raise RuntimeError("create PR requires a repo-scoped PipelineGitHub accessor")
        if self._repo_slug is not None:
            open_prs = self._open_prs_for_branch(branch)
            existing_on_branch = github_api._select_open_pr_for_base(open_prs, "main")
            if existing_on_branch is not None:
                return existing_on_branch
        existing = self.find_pr_for_issue(issue_number)
        if existing:
            return existing
        if self._skip(f"create PR for #{issue_number} from {branch!r}"):
            return 0
        if self._repo_slug is not None:
            github_api._assert_body_has_closes(body)
            github_api._assert_branch_commits_signed(branch, base="main")
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
            output = raw_output.strip()
            match = re.search(r"/pull/(\d+)", output)
            if match:
                return int(match.group(1))
            logger.error("Failed to parse PR number from gh pr create output: %r", raw_output)
            raise RuntimeError(
                f"Failed to parse PR number from gh pr create output: {raw_output!r}"
            )
        return github_api.gh_pr_create(branch, title, body)

    def mark_pr_implementation_no_go(self, pr_number: int) -> None:
        """Apply and read back exclusive ``state:implementation-no-go``."""
        if self._skip(f"mark PR #{pr_number} implementation-no-go"):
            return
        self._add_labels(pr_number, [STATE_IMPLEMENTATION_NO_GO])
        self._remove_labels(pr_number, [STATE_IMPLEMENTATION_GO])
        has_go, has_no_go = self.pr_has_implementation_state_label(pr_number)
        if has_go or not has_no_go:
            raise RuntimeError(f"PR #{pr_number} implementation-no-go label read-back failed")

    def claim_drive_green_learn(self, issue_number: int, pr_number: int) -> bool:
        """Persist and read back the pre-dispatch /learn claim.

        The claim is the exactly-once boundary for the agent's external
        learning work. A nonterminal arm record becomes ``in_progress``
        before the job is handed to the worker; a restart encountering that
        state must surface an unknown outcome instead of invoking /learn a
        second time.
        """
        if self._skip(f"claim drive-green learn for #{issue_number} (PR #{pr_number})"):
            return True
        # Hold a stable sibling lock across read/check/write/readback. The
        # JSON record is atomically replaced by save(), so it cannot itself be
        # the lock inode. Every coordinator process takes this same lock before
        # claiming, making only one external /learn dispatch possible.
        with file_lock(
            self._arming.learn_claim_lock_path(issue_number),
            require_exclusive=True,
        ):
            record = self._arming.load(issue_number) or {"pr_number": pr_number}
            status = str(record.get("learn_status") or "").lower()
            if status in {"succeeded", "failed", "in_progress"}:
                return False
            record["pr_number"] = pr_number
            record["learn_status"] = "in_progress"
            record["learn_attempted_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if not self._arming.save(issue_number, record):
                raise RuntimeError(
                    f"could not persist drive-green learn claim for issue #{issue_number}"
                )
            persisted = self._arming.load(issue_number)
            if (
                persisted is None
                or persisted.get("pr_number") != pr_number
                or persisted.get("learn_status") != "in_progress"
            ):
                raise RuntimeError(
                    f"could not verify drive-green learn claim for issue #{issue_number}"
                )
            return True

    def mark_drive_green_learn_result(self, issue_number: int, *, succeeded: bool) -> None:
        """Record the post-merge ``/learn`` outcome on the arming record.

        Mirrors ``post_merge_processor.mark_drive_green_learn_result`` (minus
        the session-evidence enrichment, which stays with the legacy driver
        until the cutover issue): written before FINISH_PASS so a restart can
        never replay ``/learn`` for the same merged PR.
        """
        if self._skip(f"record drive-green learn result for #{issue_number}"):
            return
        record = self._arming.load(issue_number) or {}
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
        if not self._arming.save(issue_number, record):
            raise RuntimeError(
                f"could not persist drive-green learn result for issue #{issue_number}"
            )
        persisted = self._arming.load(issue_number)
        if persisted is None or persisted.get("learn_status") != record["learn_status"]:
            raise RuntimeError(
                f"could not verify drive-green learn result for issue #{issue_number}"
            )

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
