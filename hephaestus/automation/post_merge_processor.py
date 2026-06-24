"""Post-merge processor extracted from CIDriver (refs #1179).

Owns the drive-green /learn and /compact steps that run after a PR
reaches green CI and auto-merge is enabled.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_session,
    uses_direct_agent_runner,
)

from .claude_invoke import invoke_claude_with_session
from .claude_models import implementer_model
from .claude_timeouts import learn_claude_timeout
from .git_utils import get_repo_slug, issue_ref, pr_ref
from .learn import build_learn_prompt, compact_session, mnemosyne_update_evidence
from .session_naming import AGENT_CI_DRIVER

logger = logging.getLogger(__name__)


class PostMergeProcessor:
    """Runs drive-green /learn and /compact after a PR merges.

    Receives all back-called CIDriver methods as callables instead of the
    full CIDriver to satisfy DIP and avoid bidirectional coupling
    (refs #1179 MAJOR finding 2).
    """

    def __init__(
        self,
        *,
        options_provider: Callable[[], Any],
        repo_root_provider: Callable[[], Path],
        get_worktree_path: Callable[[int, int], Path],
        load_arming_state: Callable[[int], dict[str, Any] | None],
        save_arming_state: Callable[[int, dict[str, Any]], None],
    ) -> None:
        """Initialise the processor with narrow provider callables.

        Args:
            options_provider: Returns the current CIDriverOptions.
            repo_root_provider: Returns the repo root Path.
            get_worktree_path: Resolves the worktree path for (issue, pr) pair.
            load_arming_state: Loads the arming record for an issue number.
            save_arming_state: Persists the arming record for an issue number.

        """
        self._options = options_provider
        self._repo_root = repo_root_provider
        self._get_worktree_path = get_worktree_path
        self._load_arming_state = load_arming_state
        self._save_arming_state = save_arming_state
        # Ephemeral evidence from the most recent /learn invocation. Reset
        # before each call and read by mark_drive_green_learn_result.
        self._last_learn_evidence: dict[str, Any] = mnemosyne_update_evidence("")

    def mark_drive_green_learn_result(
        self,
        issue_number: int,
        record: dict[str, Any],
        *,
        succeeded: bool,
    ) -> None:
        """Persist an explicit attempted/succeeded/failed learn result.

        ``learn_captured_at`` is retained as the success timestamp for older
        readers, but failure now records ``learn_status=failed`` without
        claiming Mnemosyne was updated.

        Args:
            issue_number: GitHub issue number.
            record: Mutable arming-state record dict to update in place.
            succeeded: Whether the learn session completed successfully.

        """
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        record["learn_attempted_at"] = timestamp
        if succeeded:
            record["learn_status"] = "succeeded"
            record["learn_succeeded_at"] = timestamp
            record["learn_captured_at"] = timestamp
            record.update(self._last_learn_evidence)
        else:
            record["learn_status"] = "failed"
            record["learn_succeeded_at"] = None
            record["learn_captured_at"] = None
            record.update(
                {
                    "mnemosyne_update_status": "failed",
                    "mnemosyne_update_urls": [],
                    "mnemosyne_update_pr_numbers": [],
                }
            )
        self._save_arming_state(issue_number, record)

    def run_drive_green_learnings(self, issue_number: int, pr_number: int) -> bool:
        """Capture drive-green learnings under AGENT_CI_DRIVER (Session 3).

        Runs after a PR reaches green and auto-merge is enabled, mirroring the
        implementer's post-PR ``/learn`` step but scoped to *this* drive: what
        made CI fail and how it was fixed. Resumes Session 3 (the
        ``AGENT_CI_DRIVER`` session the fix session created) so the learnings
        compound on the same transcript that did the work.

        This is best-effort. Any failure is logged at WARNING and swallowed so
        a flaky learnings step never flips a successful drive to failure.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            True if the learnings session completed, False otherwise.

        """
        options = self._options()
        repo_root = self._repo_root()
        prompt = build_learn_prompt(
            f"You just drove PR {pr_ref(pr_number)} (issue {issue_ref(issue_number)}) "
            "to green CI. Capture concise learnings about what made CI fail and how "
            "you fixed it, scoped to this issue/PR."
        )
        self._last_learn_evidence = mnemosyne_update_evidence("")
        try:
            repo_slug = get_repo_slug(repo_root)
            # Best-effort: try to resume in the original worktree (so the
            # AGENT_CI_DRIVER transcript is found by ``session_jsonl_path``).
            # In the post-merge code path (#840) the PR's head branch may be
            # gone from the remote, so ``_get_worktree_path`` could fail. In
            # that case fall back to ``repo_root`` cwd — the prompt is
            # self-contained, and a fresh ``--session-id`` create still
            # captures the lesson.
            try:
                cwd = self._get_worktree_path(issue_number, pr_number)
            except Exception as wt_err:
                logger.info(
                    "Issue #%s: no worktree available for /learn (%s); "
                    "falling back to repo root for a fresh session",
                    issue_number,
                    wt_err,
                )
                cwd = repo_root
            if uses_direct_agent_runner(options.agent):
                direct_result = run_agent_session(
                    agent=options.agent,
                    prompt=prompt,
                    cwd=cwd,
                    timeout=learn_claude_timeout(),
                    model=direct_agent_model(options.agent, "HEPH_LEARN_MODEL"),
                    sandbox="workspace-write",
                )
                self._last_learn_evidence = mnemosyne_update_evidence(direct_result.stdout or "")
                logger.info(
                    "Issue #%s: drive-green learnings captured with %s",
                    issue_number,
                    options.agent,
                )
                return True
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_CI_DRIVER,
                prompt=prompt,
                # /learn inherits the parent phase's model. drive-green resumes
                # the implementer's session, and ``claude --resume`` is locked to
                # the model that created it, so we must use implementer_model().
                model=implementer_model(),
                cwd=cwd,
                timeout=learn_claude_timeout(),
                output_format="text",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
                extra_args=["--dangerously-skip-permissions"],
                input_via_stdin=True,
            )
            self._last_learn_evidence = mnemosyne_update_evidence(stdout or "")
            logger.info("Issue #%s: drive-green learnings captured", issue_number)
            return True
        except Exception as e:  # broad: external claude process; non-blocking
            logger.warning(
                "Issue #%s: drive-green learnings failed (non-fatal): %s",
                issue_number,
                e,
            )
            return False

    def run_drive_green_compact(self, issue_number: int, pr_number: int) -> bool:
        """Compact the AGENT_CI_DRIVER session transcript after /learn (#842).

        Mirrors the cwd-derivation of ``run_drive_green_learnings``: try the
        worktree first so the deterministic JSONL probe in ``session_jsonl_path``
        finds the transcript, fall back to ``repo_root`` when the branch is
        already gone post-merge. Non-fatal.

        Args:
            issue_number: GitHub issue number.
            pr_number: GitHub PR number.

        Returns:
            True if the compact session completed, False otherwise.

        """
        options = self._options()
        repo_root = self._repo_root()
        if uses_direct_agent_runner(options.agent):
            logger.info(
                "Issue #%s: skipping /compact (%s does not use Claude compact sessions)",
                issue_number,
                options.agent,
            )
            return False
        try:
            cwd = self._get_worktree_path(issue_number, pr_number)
        except Exception as wt_err:
            logger.info(
                "Issue #%s: no worktree available for /compact (%s); using repo root",
                issue_number,
                wt_err,
            )
            cwd = repo_root
        repo_slug = get_repo_slug(repo_root)
        return compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_CI_DRIVER,
            cwd=cwd,
            model=implementer_model(),
        )
