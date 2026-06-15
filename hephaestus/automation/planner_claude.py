"""Claude/Codex invocation helpers for :class:`Planner`.

Wraps :func:`hephaestus.automation.claude_invoke.invoke_claude_with_session`
(and the equivalent Codex CLI runner) with the rate-limit-retry policy that
the planner has historically applied. Extracted from ``planner.py`` (#598)
so the coordinator class stays focused on the worker-pool driver. No
behavior change.
"""

from __future__ import annotations

import logging
import subprocess
import time
from typing import TYPE_CHECKING

from hephaestus.agents.runtime import is_codex, run_codex_text
from hephaestus.github.rate_limit import wait_until

from .claude_invoke import detect_server_overload, invoke_claude_with_session, scan_quota_reset
from .git_utils import get_repo_root, get_repo_slug

if TYPE_CHECKING:
    from .models import PlannerOptions

logger = logging.getLogger(__name__)

# Base delay (seconds) for the exponential backoff applied to transient
# server-overload (529 / 5xx) retries. The Nth retry (counting down from the
# default ``max_retries=3``) waits ``_OVERLOAD_BACKOFF_BASE_S * 2 ** (3 -
# max_retries)`` seconds, i.e. 5s, 10s, 20s. Unlike a 429 quota cap there is no
# reset epoch to wait on, so a bounded exponential backoff is the correct
# policy (#1374).
_OVERLOAD_BACKOFF_BASE_S = 5.0
# Default retry budget the backoff schedule is anchored to; used only to size
# the per-attempt delay so it grows monotonically as retries are consumed.
_OVERLOAD_BACKOFF_ANCHOR_RETRIES = 3


class PlannerClaudeRunner:
    """Run Claude or Codex on behalf of the planner.

    Holds a reference to :class:`PlannerOptions` so it can pick the right
    backend (Claude vs Codex) and forward the optional system-prompt file.
    """

    def __init__(self, options: PlannerOptions) -> None:
        """Bind to the planner's options.

        Args:
            options: The shared :class:`PlannerOptions` instance.

        """
        self.options = options

    def call_claude(
        self,
        prompt: str,
        *,
        model: str,
        agent: str,
        issue_number: int | str,
        max_retries: int = 3,
        timeout: int = 300,
        extra_args: list[str] | None = None,
    ) -> str:
        """Call Claude CLI on a deterministic session with rate-limit retry.

        The session UUID is derived from ``(repo, issue_number, agent,
        trunk_githash)`` via :func:`session_naming.session_uuid`. First call
        for a tuple creates the session; every later call resumes it. Cross-
        agent independence (planner vs reviewer) is preserved because the
        ``agent`` string is part of the hash.

        Args:
            prompt: The prompt to send to Claude.
            model: Claude model ID for ``--model`` (caller picks per phase).
            agent: One of the ``AGENT_*`` constants from
                :mod:`hephaestus.automation.session_naming`. Different agents
                map to different session IDs.
            issue_number: GitHub issue number; participates in the session ID.
            max_retries: Maximum retry attempts for rate limits.
            timeout: Subprocess timeout in seconds.
            extra_args: Additional CLI arguments.

        Returns:
            Claude's response text (stdout, stripped).

        Raises:
            RuntimeError: If Claude call fails.

        """
        if is_codex(self.options.agent):
            return self.call_codex(prompt, model=model, max_retries=max_retries, timeout=timeout)

        repo_root = get_repo_root()
        repo = get_repo_slug(repo_root)

        try:
            stdout, _ = invoke_claude_with_session(
                repo=repo,
                issue=issue_number,
                agent=agent,
                prompt=prompt,
                model=model,
                cwd=repo_root,
                timeout=timeout,
                system_prompt_file=self.options.system_prompt_file,
                allowed_tools="Read,Glob,Grep,Bash",
                extra_args=extra_args,
            )
            response = stdout.strip()
            if not response:
                raise RuntimeError("Claude returned empty response")
            return response

        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""

            # Rate-limit messages may appear in either stream. The Claude CLI
            # in particular returns its 429 ("You're out of extra usage ·
            # resets ...") inside the stdout JSON payload as the ``result``
            # field of an ``is_error: true`` response, not in stderr.
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and max_retries > 0:
                if reset_epoch > 0:
                    wait_until(reset_epoch)
                else:
                    time.sleep(5)
                return self.call_claude(
                    prompt,
                    model=model,
                    agent=agent,
                    issue_number=issue_number,
                    max_retries=max_retries - 1,
                    timeout=timeout,
                    extra_args=extra_args,
                )

            # A transient server-overload (529 Overloaded / generic 5xx) carries
            # no reset epoch, so scan_quota_reset misses it. It used to fall
            # straight through to the fatal raise below, surfacing a recoverable
            # blip as ``phase plan FAILED rc=1`` (#1374). Retry it with bounded
            # exponential backoff up to ``max_retries``.
            if detect_server_overload(stderr, stdout) and max_retries > 0:
                # Exponent grows as retries are consumed (clamped at 0 so an
                # over-budget caller never gets a fractional/negative delay).
                exponent = max(0, _OVERLOAD_BACKOFF_ANCHOR_RETRIES - max_retries)
                delay = _OVERLOAD_BACKOFF_BASE_S * (2**exponent)
                logger.warning(
                    "Claude server overloaded (transient); retrying in %.0fs (%d retries left)",
                    delay,
                    max_retries,
                )
                time.sleep(delay)
                return self.call_claude(
                    prompt,
                    model=model,
                    agent=agent,
                    issue_number=issue_number,
                    max_retries=max_retries - 1,
                    timeout=timeout,
                    extra_args=extra_args,
                )

            detail = stderr or stdout or "(no output)"
            raise RuntimeError(f"Claude failed: {detail}") from e

        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Claude timed out after {timeout}s") from e

    def call_codex(
        self,
        prompt: str,
        *,
        model: str,
        max_retries: int = 3,
        timeout: int = 300,
    ) -> str:
        """Call Codex CLI with retry logic for rate limits."""
        try:
            result = run_codex_text(
                prompt,
                cwd=get_repo_root(),
                timeout=timeout,
                sandbox="workspace-write",
            )
        except subprocess.CalledProcessError as e:
            stderr = e.stderr or ""
            stdout = e.stdout or ""
            reset_epoch = scan_quota_reset(stderr, stdout)
            if reset_epoch is not None and reset_epoch > 0 and max_retries > 0:
                logger.warning("Codex usage cap hit; waiting for reset")
                wait_until(reset_epoch)
                return self.call_codex(
                    prompt,
                    model=model,
                    max_retries=max_retries - 1,
                    timeout=timeout,
                )
            detail = stderr or stdout or str(e)
            raise RuntimeError(f"Codex failed: {detail}") from e
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"Codex timed out after {timeout}s") from e

        response = (result.stdout or "").strip()
        if not response:
            raise RuntimeError("Codex returned empty response")
        return response
