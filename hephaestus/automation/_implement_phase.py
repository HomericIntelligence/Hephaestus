"""Implementation (advise + agent invocation) phase.

Extracted from :class:`ImplementationPhaseRunner` as part of the #712
decomposition. :class:`ImplementPhase` owns the advise-first lookup and the
selected-agent (Claude/Codex) implementation session — the work that turns a
GO plan into committed code on the issue branch.

The two module-level helpers ``_prepend_advise`` and
``_claude_quota_reset_epoch`` live here because the implementation path is
their only caller.
"""

from __future__ import annotations

import contextlib
import json
import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, cast

from hephaestus.agents.runtime import (
    is_codex,
    run_codex_session,
    run_codex_text,
)
from hephaestus.github.rate_limit import wait_until

from ._stage_context import StageMixin
from .advise_runner import run_advise
from .claude_invoke import invoke_claude_with_session
from .claude_models import advise_model, implementer_model
from .claude_timeouts import advise_claude_timeout, implementer_claude_timeout
from .git_utils import get_repo_slug
from .learn import compact_session
from .prompts import get_advise_prompt_builder
from .session_naming import AGENT_ADVISE, AGENT_IMPLEMENTER

if TYPE_CHECKING:
    from ._stage_context import StageContext

logger = logging.getLogger(__name__)


def _prepend_advise(advise_findings: str, prompt: str) -> str:
    """Prepend advise findings as a context block to an implementation prompt.

    Returns ``prompt`` unchanged when there are no real findings — an empty
    string or an ``advise_runner.advise_skipped`` HTML-comment marker (which
    records *why* advise produced nothing) carries no guidance worth injecting.
    """
    findings = advise_findings.strip()
    if not findings or findings.startswith("<!-- advise step skipped"):
        return prompt
    return f"## Prior Learnings from Team Knowledge Base\n\n{findings}\n\n---\n\n{prompt}"


def _claude_quota_reset_epoch(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Thin wrapper over the single common resolver
    :func:`hephaestus.github.rate_limit.resolve_quota_reset_epoch` (#1321) so
    every agent-call path shares one detection surface — including the Claude
    session-limit 429 phrasing that the older two-detector logic missed.
    """
    from hephaestus.github.rate_limit import resolve_quota_reset_epoch

    return resolve_quota_reset_epoch(*texts)


class ImplementPhase(StageMixin):
    """Run advise + the selected implementation agent for one issue."""

    def __init__(self, ctx: StageContext) -> None:
        """Store the shared :class:`StageContext`."""
        self.ctx = ctx

    def _run_advise(self, issue_number: int, issue_title: str, issue_body: str) -> str:
        """Search ProjectMnemosyne for prior learnings — planner's separate-session path.

        Used by the planner (and Codex) where advise runs under ``AGENT_ADVISE``
        (a distinct, cheap, read-only session) and returns text findings for the
        caller to inject into its own prompt context.  Claude implementer sessions
        use :meth:`_run_advise_as_implementer_turn` instead, which makes advise
        the *first turn* of the implementer's own session so the findings live in
        the transcript and inform the implementation turn directly.
        """

        def _invoke(prompt: str) -> str:
            if is_codex(self.options.agent):
                result = run_codex_text(
                    prompt,
                    cwd=self.repo_root,
                    timeout=advise_claude_timeout(),
                    sandbox="read-only",
                )
                return (result.stdout or "").strip()
            repo_slug = get_repo_slug(self.repo_root)
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_ADVISE,
                prompt=prompt,
                model=advise_model(),
                cwd=self.repo_root,
                timeout=advise_claude_timeout(),
                output_format="text",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=get_advise_prompt_builder(self.options.agent),
        )

    def _run_advise_as_implementer_turn(
        self,
        issue_number: int,
        issue_title: str,
        issue_body: str,
        worktree_path: Path,
    ) -> str:
        """Send the advise prompt as the first turn of the implementer's Claude session.

        Unlike :meth:`_run_advise`, which creates a separate ``AGENT_ADVISE``
        session and returns text, this method sends the advise prompt directly to
        ``AGENT_IMPLEMENTER`` (using ``cwd=worktree_path`` so the session transcript
        is co-located with the subsequent implementation turn).  The advise findings
        live in the implementer's own transcript, so turn 2 (the implementation
        prompt) automatically inherits them via ``--resume`` — no text injection
        needed.

        Codex does not support this two-turn flow; callers must guard with
        ``is_codex`` and fall back to :meth:`_run_advise` + text injection for
        Codex agents.

        On first run ``invoke_claude_with_session`` auto-creates the session
        (``transcript.exists()`` is False).  On subsequent loops the same
        deterministic session UUID auto-resumes so the advise findings accumulate
        across iterations.

        Any failure degrades gracefully inside ``run_advise`` and returns an
        empty string, so the caller can still proceed to the implementation turn.
        """
        repo_slug = get_repo_slug(self.repo_root)

        # Fetch plan and plan-review from GitHub comments to give advise the full
        # context of what's been planned (same anchored selection as
        # _fetch_plan_and_review so PLAN_REVIEW comments are distinguished from
        # the PLAN comment).
        plan_text, plan_review_text = self.runner._fetch_plan_and_review(issue_number)

        def _build_prompt_with_plan(**kw: object) -> str:
            # Claude runs with cwd=worktree_path, so pass worktree_path as the
            # relativization root.  marketplace.json lives under build/ in the main
            # repo, not under the worktree, so _relativize_path will fall back to
            # the absolute path — which is always readable regardless of cwd.
            kw["repo_root"] = str(worktree_path)
            base_prompt = get_advise_prompt_builder(self.options.agent)(**kw)
            if not plan_text and not plan_review_text:
                return base_prompt
            parts = []
            if plan_text:
                parts.append(f"## Implementation Plan\n\n{plan_text}")
            if plan_review_text:
                parts.append(f"## Plan Review\n\n{plan_review_text}")
            plan_block = "\n\n".join(parts)
            return f"{plan_block}\n\n---\n\n{base_prompt}"

        def _invoke(prompt: str) -> str:
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_IMPLEMENTER,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=advise_claude_timeout(),
                output_format="text",
            )
            return (stdout or "").strip()

        return run_advise(
            issue_number=issue_number,
            issue_title=issue_title,
            issue_body=issue_body,
            invoke=_invoke,
            build_prompt=_build_prompt_with_plan,
        )

    def _compact_implementer_session(self, issue_number: int, worktree_path: Path) -> None:
        """Compact the implementer session after /learn (#842). Non-fatal."""
        repo_slug = get_repo_slug(self.repo_root)
        compact_session(
            repo=repo_slug,
            issue=issue_number,
            agent=AGENT_IMPLEMENTER,
            cwd=worktree_path,
            model=implementer_model(),
        )

    def _run_claude_code(
        self, issue_number: int, worktree_path: Path, prompt: str, slot_id: int | None = None
    ) -> str | None:
        """Run the selected implementation agent in a worktree."""
        if self.options.dry_run:
            logger.info("[DRY RUN] Would run %s for issue #%s", self.options.agent, issue_number)
            return None

        self.state_dir.mkdir(parents=True, exist_ok=True)

        if is_codex(self.options.agent):
            return self.impl._run_codex_code(issue_number, worktree_path, prompt)

        return self.impl._run_claude_impl_session(issue_number, worktree_path, prompt)

    def _run_claude_impl_session(
        self, issue_number: int, worktree_path: Path, prompt: str
    ) -> str | None:
        """Run Claude implementation prompt and return its session id."""
        prompt_file = worktree_path / f".claude-prompt-{issue_number}.md"
        prompt_file.write_text(prompt)

        repo_slug = get_repo_slug(self.repo_root)

        try:
            stdout, _ = invoke_claude_with_session(
                repo=repo_slug,
                issue=issue_number,
                agent=AGENT_IMPLEMENTER,
                prompt=prompt,
                model=implementer_model(),
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                output_format="json",
                permission_mode="dontAsk",
                allowed_tools="Read,Write,Edit,Glob,Grep,Bash",
            )
            result = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")
            # Parse session_id from JSON output
            try:
                data = json.loads(result.stdout)

                # The CLI sometimes returns exit 0 with ``is_error: true`` in
                # JSON (e.g. usage caps in some channels). Treat that as a
                # failure so the orchestrator can wait/retry instead of
                # silently logging a useless session_id.
                if isinstance(data, dict) and data.get("is_error"):
                    err_text = str(data.get("result") or "")
                    log_file = self.state_dir / f"claude-{issue_number}.log"
                    log_file.write_text(result.stdout or "")
                    reset_epoch = _claude_quota_reset_epoch(err_text)
                    if reset_epoch is not None and reset_epoch > 0:
                        logger.warning(
                            "Claude usage cap hit for issue #%s; waiting for reset", issue_number
                        )
                        wait_until(reset_epoch)
                    raise RuntimeError(f"Claude Code failed: {err_text or 'is_error=true'}")

                session_id = data.get("session_id")

                # Save successful output to log file
                log_file = self.state_dir / f"claude-{issue_number}.log"
                log_file.write_text(result.stdout or "")

                return cast("str | None", session_id)
            except (json.JSONDecodeError, AttributeError):
                logger.warning("Could not parse session_id for issue #%s", issue_number)
                logger.debug("Claude stdout: %s", result.stdout[:500])

                # Save output even if JSON parsing failed
                log_file = self.state_dir / f"claude-{issue_number}.log"
                log_file.write_text(result.stdout or "")

                return None
        except subprocess.CalledProcessError as e:
            logger.error("Claude Code failed for issue #%s", issue_number)
            logger.error("Exit code: %s", e.returncode)
            if e.stdout:
                logger.error("Stdout: %s", e.stdout[:1000])
            if e.stderr:
                logger.error("Stderr: %s", e.stderr[:1000])

            # Save failure output to log file
            log_file = self.state_dir / f"claude-{issue_number}.log"
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(output)

            # If the failure was a quota cap, block until reset rather than
            # letting the orchestrator burn through every remaining issue in
            # seconds. The Claude CLI puts its 429 message in stdout JSON.
            reset_epoch = _claude_quota_reset_epoch(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0:
                logger.warning(
                    "Claude usage cap hit for issue #%s; waiting for reset", issue_number
                )
                wait_until(reset_epoch)

            raise RuntimeError(f"Claude Code failed: {e.stderr or e.stdout}") from e
        except subprocess.TimeoutExpired as e:
            # Save timeout info to log file
            log_file = self.state_dir / f"claude-{issue_number}.log"
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")

            raise RuntimeError("Claude Code timed out") from e
        finally:
            # Clean up temp file
            with contextlib.suppress(Exception):
                prompt_file.unlink()

    def _run_codex_code(self, issue_number: int, worktree_path: Path, prompt: str) -> str | None:
        """Run Codex implementation prompt in a worktree."""
        log_file = self.state_dir / f"codex-{issue_number}.log"
        try:
            result = run_codex_session(
                prompt,
                cwd=worktree_path,
                timeout=implementer_claude_timeout(),
                sandbox="workspace-write",
            )
            log_file.write_text(result.stdout or "")
            return result.session_id
        except subprocess.CalledProcessError as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""
            output = f"EXIT CODE: {e.returncode}\n\nSTDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
            log_file.write_text(output)
            reset_epoch = _claude_quota_reset_epoch(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0:
                logger.warning("Codex usage cap hit for issue #%s; waiting for reset", issue_number)
                wait_until(reset_epoch)
            raise RuntimeError(f"Codex failed: {stderr or stdout}") from e
        except subprocess.TimeoutExpired as e:
            log_file.write_text(f"TIMEOUT after {e.timeout}s\n\nOutput:\n{e.output or ''}")
            raise RuntimeError("Codex timed out") from e
