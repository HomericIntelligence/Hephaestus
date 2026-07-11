"""High-level CI-fix attempt flow for drive-green."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    run_agent_session,
    uses_direct_agent_runner,
)

from .advise_runner import run_advise
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, codex_advise_model
from .git_utils import get_repo_slug, issue_ref
from .models import CIDriverOptions, WorkerResult
from .prompts import get_advise_prompt_builder
from .session_naming import AGENT_ADVISE

logger = logging.getLogger(__name__)


class CIFixFlow:
    """Coordinates advise, CI-fix sessions, markers, and bot-thread cleanup."""

    def __init__(
        self,
        *,
        options_provider: Callable[[], CIDriverOptions],
        repo_root_provider: Callable[[], Path],
        status_tracker_provider: Callable[[], Any],
        orchestrator: Any,
        markers: Any,
        review_threads: Any,
        is_bot_pr_mode: Callable[[int, int], bool],
        gh_issue_json: Callable[[int], dict[str, Any]],
        get_failing_ci_logs: Callable[[int], str],
        load_impl_session_id: Callable[[int], str | None],
        get_worktree_path: Callable[[int, int], Path],
        get_pr_branch: Callable[[int], str],
    ) -> None:
        """Initialise high-level CI-fix dependencies."""
        self._options = options_provider
        self._repo_root = repo_root_provider
        self._status = status_tracker_provider
        self._orchestrator = orchestrator
        self._markers = markers
        self._review_threads = review_threads
        self._is_bot_pr_mode = is_bot_pr_mode
        self._gh_issue_json = gh_issue_json
        self._get_failing_ci_logs = get_failing_ci_logs
        self._load_impl_session_id = load_impl_session_id
        self._get_worktree_path = get_worktree_path
        self._get_pr_branch = get_pr_branch

    def run_advise(self, issue_number: int) -> str:
        """Pull prior learnings from Mnemosyne before a CI-fix loop."""
        issue_data = self._gh_issue_json(issue_number)
        issue_title = issue_data.get("title", f"Issue #{issue_number}")
        issue_body = issue_data.get("body", "")

        def invoke(prompt: str) -> str:
            if uses_direct_agent_runner(self._options().agent):
                result = run_agent_session(
                    agent=self._options().agent,
                    prompt=prompt,
                    cwd=self._repo_root(),
                    timeout=self._options().advise_timeout,
                    model=direct_agent_model(
                        self._options().agent,
                        "HEPH_ADVISE_MODEL",
                        codex_default=codex_advise_model(),
                    ),
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            stdout, _ = invoke_claude_with_session(
                repo=get_repo_slug(self._repo_root()),
                issue=issue_number,
                agent=AGENT_ADVISE,
                prompt=prompt,
                model=advise_model(),
                cwd=self._repo_root(),
                timeout=self._options().advise_timeout,
                output_format="text",
                allowed_tools="Read,Glob,Grep,Bash",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=invoke,
            build_prompt=get_advise_prompt_builder(self._options().agent),
        )

    def attempt_ci_fixes(
        self,
        issue_number: int,
        pr_number: int,
        acquired_slot: int,
        extra_context: str = "",
    ) -> WorkerResult | None:
        """Attempt CI-fix iterations for a failing PR."""
        if not self._options().dry_run and self._markers.already_pushed_for_current_head(
            issue_number, pr_number
        ):
            logger.info(
                "Issue #%s: PR #%s head unchanged since the last CI fix this driver pushed; "
                "skipping redundant CI-fix agent and awaiting pending CI",
                issue_number,
                pr_number,
            )
            return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
        advise_findings = ""
        if self._options().enable_advise and not self._is_bot_pr_mode(issue_number, pr_number):
            self._status().update_slot(acquired_slot, f"{issue_ref(issue_number)}: advising")
            advise_findings = self.run_advise(issue_number)
        for iteration in range(self._options().max_fix_iterations):
            self._status().update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: fetching CI logs (attempt {iteration + 1})",
            )
            ci_logs = self._get_failing_ci_logs(pr_number)
            if extra_context:
                ci_logs = f"{extra_context}\n\n{ci_logs}".strip()
            session_id = self._load_impl_session_id(issue_number)
            worktree_path = self._get_worktree_path(issue_number, pr_number)
            pr_head_branch = self._get_pr_branch(pr_number)
            if self._options().dry_run:
                logger.info(
                    "[dry_run] Would run CI fix session for PR #%s (issue #%s, iteration %s)",
                    pr_number,
                    issue_number,
                    iteration + 1,
                )
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
            self._status().update_slot(
                acquired_slot,
                f"{issue_ref(issue_number)}: running CI fix session (attempt {iteration + 1})",
            )
            fixed = self._orchestrator.run_ci_fix_session(
                issue_number,
                pr_number,
                worktree_path,
                ci_logs,
                session_id,
                advise_findings,
                pr_head_branch=pr_head_branch,
            )
            if fixed:
                logger.info("Issue #%s: CI fix applied successfully", issue_number)
                self._markers.record_head(pr_number)
                self._review_threads.reply_and_resolve_bot_threads(pr_number)
                return WorkerResult(issue_number=issue_number, success=True, pr_number=pr_number)
            logger.warning("Issue #%s: CI fix attempt %s failed", issue_number, iteration + 1)
        return None
