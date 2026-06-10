"""Learn lifecycle functions for issue implementation.

Provides:
- Running /learn skill in Claude sessions
- Checking if learn needs re-run
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from hephaestus.agents.runtime import resume_codex_session, session_agent_matches

from .claude_models import learn_model
from .claude_timeouts import learn_claude_timeout
from .git_utils import run
from .session_naming import session_uuid

logger = logging.getLogger(__name__)


def build_learn_prompt(context: str) -> str:
    """Return the standard automation prompt for the user-facing /learn skill."""
    detail = context.strip()
    if detail:
        detail = f" {detail}"
    return (
        f"/learn{detail}"
        " Commit the results and create a PR."
        " IMPORTANT: Only push skills to ProjectMnemosyne."
        " Do NOT create files under .claude-plugin/ in this repo."
    )


def run_learn(
    session_id: str,
    worktree_path: Path,
    issue_number: int,
    state_dir: Path,
    slot_id: int | None = None,
    agent: str = "claude",
    session_agent: str | None = None,
    model: str | None = None,
) -> bool:
    """Resume agent session to run /learn.

    Args:
        session_id: Agent session ID
        worktree_path: Path to worktree
        issue_number: Issue number
        state_dir: Directory for state/log files
        slot_id: Worker slot ID (unused; kept for interface symmetry)
        model: Override the model used for /learn. When ``None`` (default)
            the configured ``HEPH_LEARN_MODEL`` / ``learn_model()`` is used.
            Pass ``implementer_model()`` so the implementer's /learn turn runs
            on the same model tier the session was created with.

    Returns:
        True if learn completed successfully, False otherwise

    Runs from worktree directory so Claude can find the session.
    Output is logged to state_dir/learn-{issue_number}.log.

    """
    state_dir.mkdir(parents=True, exist_ok=True)
    log_file = state_dir / f"learn-{issue_number}.log"
    if not session_agent_matches(session_agent, agent):
        message = (
            f"Session belongs to {session_agent or 'claude'}, "
            f"but selected agent is {agent}; skipping learn resume"
        )
        logger.warning("Learn skipped for issue #%s: %s", issue_number, message)
        log_file.write_text(f"FAILED: {message}\n")
        return False

    if agent == "codex":
        try:
            codex_result = resume_codex_session(
                session_id,
                build_learn_prompt(""),
                cwd=worktree_path,
                timeout=learn_claude_timeout(),
            )
            log_file.write_text(codex_result.stdout)
            logger.info("Learn completed for issue #%s", issue_number)
            logger.info("Learn log: %s", log_file)
            return True
        except Exception as e:  # broad catch: external agent process; non-blocking
            logger.warning("Learn failed for issue #%s: %s", issue_number, e)
            log_file.write_text(f"FAILED: {e}\n")
            return False

    # /learn is a SIMPLE-complexity task (summarization + file writes), so we
    # use the configured learn model (default: Haiku) but accept operator
    # overrides via HEPH_LEARN_MODEL. Callers may pass `model` explicitly to
    # run /learn on the same model tier as the session (e.g. implementer_model()).
    # We can't route through `call_claude` here because we need `--resume`
    # semantics with full Bash/Edit tools; instead we add the model flag directly.
    effective_model = model if model is not None else learn_model()
    try:
        result = run(
            [
                "claude",
                "--resume",
                session_id,
                build_learn_prompt(""),
                "--print",
                "--model",
                effective_model,
                "--permission-mode",
                "dontAsk",
                "--allowedTools",
                "Read,Write,Edit,Glob,Grep,Bash",
            ],
            cwd=worktree_path,
            timeout=learn_claude_timeout(),
        )
        # Write output to log file
        log_file.write_text(result.stdout or "")
        logger.info("Learn completed for issue #%s", issue_number)
        logger.info("Learn log: %s", log_file)
        return True
    except Exception as e:  # broad catch: external claude process; non-blocking, must not propagate
        logger.warning("Learn failed for issue #%s: %s", issue_number, e)

        # Save failure output to log file
        error_output = f"FAILED: {e}\n"
        if hasattr(e, "stdout"):
            error_output += f"\nSTDOUT:\n{e.stdout or ''}"
        if hasattr(e, "stderr"):
            error_output += f"\nSTDERR:\n{e.stderr or ''}"
        log_file.write_text(error_output)

        # Non-blocking: never re-raise
        return False


def learn_needs_rerun(issue_number: int, state_dir: Path) -> bool:
    """Check if learn log indicates failure.

    Args:
        issue_number: Issue number
        state_dir: Directory containing learn log files

    Returns:
        True if learn needs to be re-run (missing or failed log)

    """
    log_file = state_dir / f"learn-{issue_number}.log"
    if not log_file.exists():
        return True
    try:
        content = log_file.read_text()
        return content.startswith("FAILED:")
    except OSError:
        return True


def compact_session(
    repo: str,
    issue: int | str,
    agent: str,
    cwd: Path,
    timeout: int = 60,
) -> bool:
    """Send ``/compact`` to the (repo, issue, agent) Claude session.

    Best-effort transcript summarisation. Fires immediately after ``/learn``
    on a durably-done stage so the next ``--resume`` reads a summary instead
    of the full fix-iteration replay (#842).

    Non-fatal: any failure (timeout, missing binary, non-zero exit) is logged
    at WARNING and swallowed; the next resume just pays full-history cost.

    Args:
        repo: Repository slug
        issue: Issue number
        agent: Agent identifier
        cwd: Working directory for session lookup
        timeout: Subprocess timeout in seconds (default: 60)

    Returns:
        True on a zero-exit subprocess call, False on any failure including
        a non-zero exit code (e.g. /compact skill not registered).

    """
    sid = session_uuid(repo, issue, agent)
    try:
        result = subprocess.run(
            [
                "claude",
                "--resume",
                sid,
                "--output-format",
                "text",
                "--dangerously-skip-permissions",
                "--print",
                "/compact",
            ],
            cwd=str(cwd),
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(
            "Issue #%s: /compact failed for agent=%s (non-fatal): %s",
            issue,
            agent,
            e,
        )
        return False

    if result.returncode != 0:
        logger.warning(
            "Issue #%s: /compact for agent=%s exited %s (non-fatal); stderr=%s",
            issue,
            agent,
            result.returncode,
            (result.stderr or "")[:200],
        )
        return False

    logger.info("Issue #%s: /compact completed for agent=%s (session %s)", issue, agent, sid)
    return True
