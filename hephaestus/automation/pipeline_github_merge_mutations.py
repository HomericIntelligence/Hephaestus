"""Own the native auto-merge mutations for one pull request.

The module arms and disarms GitHub native auto-merge. GitHub owns branch
protection, required checks, merge queues, and conflicts.
"""

import subprocess
import time
from threading import Event

from .pipeline_github_comments import PipelineGitHubIssueComments
from .pipeline_github_transport import (
    AutoMergeEnableResult,
    logger,
)


class PipelineGitHubMergeMutations(PipelineGitHubIssueComments):
    """Own the coordinator-approved merge mutations for one pull request."""

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

    def disable_auto_merge(self, pr_number: int) -> bool:
        """Disable native auto-merge for one pull request.

        Called when ``state:implementation-no-go`` is applied, so a withdrawn
        approval cannot merge. A pull request without auto-merge armed is
        already in the wanted state. Returns True when auto-merge is off.
        """
        if pr_number <= 0 or self._skip(f"disable auto-merge for PR #{pr_number}"):
            return False
        try:
            result = self._deadline_gh_call(
                ["pr", "merge", str(pr_number), "--disable-auto"],
                check=False,
                retry_on_rate_limit=False,
                max_retries=1,
                timeout=float(self._gh_timeout),
            )
        except (subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: auto-merge disable failed: %s", pr_number, exc)
            return False
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").lower()
        if "not enabled" in stderr or ("auto-merge" in stderr and "disabled" in stderr):
            return True
        logger.warning("PR #%s: auto-merge disable failed: %s", pr_number, stderr.strip())
        return False
