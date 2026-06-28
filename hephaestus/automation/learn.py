"""Learn lifecycle functions for issue implementation.

Provides:
- Running /learn skill in Claude sessions
- Checking if learn needs re-run
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hephaestus.agents.runtime import (
    direct_agent_model,
    resume_agent_session,
    session_agent_matches,
    uses_direct_agent_runner,
)
from hephaestus.github.rate_limit import resolve_quota_reset_epoch, wait_until
from hephaestus.io.utils import write_secure

from ._review_utils import log_file_path
from .claude_models import learn_model
from .claude_timeouts import DEFAULT_AGENT_TIMEOUT, learn_claude_timeout
from .git_utils import run
from .session_naming import session_uuid

logger = logging.getLogger(__name__)

# Bound the wait-and-retry loop in :func:`run_learn`. A rate-limited ``/learn``
# must wait for the quota reset and re-invoke rather than silently dropping the
# learning (#1331), but a misdetected rate-limit message must not spin forever.
_LEARN_RATE_LIMIT_MAX_RETRIES = 5

# Fixed back-off (seconds) applied when ``resolve_quota_reset_epoch`` returns the
# ``0`` "rate-limited, reset unknown" sentinel. Waiting a few minutes lets a
# transient/secondary limit clear without busy-looping on an unknown deadline.
_LEARN_UNKNOWN_RESET_BACKOFF_SECONDS = 300

# Owner-agnostic: the Mnemosyne target may be the upstream
# (HomericIntelligence/ProjectMnemosyne) or any user's fork
# (<login>/ProjectMnemosyne). Only the repo name is fixed.
_MNEMOSYNE_URL_RE = re.compile(
    r"https://github\.com/[A-Za-z0-9._-]+/ProjectMnemosyne/(?:pull|commit)/[A-Za-z0-9._/-]+"
)
_MNEMOSYNE_PR_REF_RE = re.compile(r"\b[A-Za-z0-9._-]+/ProjectMnemosyne#(?P<number>\d+)\b")


def mnemosyne_update_evidence(output: str) -> dict[str, Any]:
    """Extract ProjectMnemosyne update evidence from a ``/learn`` response.

    A successful agent turn is not proof that ProjectMnemosyne changed. Treat
    concrete ProjectMnemosyne PR/commit URLs or owner/repo issue-style PR refs
    as confirmation; otherwise mark the update as unverified.
    """
    text = output if isinstance(output, str) else str(output or "")
    urls = sorted(set(_MNEMOSYNE_URL_RE.findall(text)))
    pr_numbers = sorted({int(m.group("number")) for m in _MNEMOSYNE_PR_REF_RE.finditer(text)})
    status = "confirmed" if urls or pr_numbers else "unverified"
    return {
        "mnemosyne_update_status": status,
        "mnemosyne_update_urls": urls,
        "mnemosyne_update_pr_numbers": pr_numbers,
    }


def _write_learn_record(
    state_dir: Path,
    issue_number: int,
    *,
    succeeded: bool,
    log_file: Path,
    output: str = "",
    error: str = "",
) -> None:
    """Persist explicit implementer ``/learn`` attempt evidence."""
    timestamp = datetime.now(timezone.utc).isoformat()
    record: dict[str, object] = {
        "issue_number": issue_number,
        "learn_attempted_at": timestamp,
        "learn_status": "succeeded" if succeeded else "failed",
        "learn_succeeded_at": timestamp if succeeded else None,
        "log_path": str(log_file),
    }
    if succeeded:
        record.update(mnemosyne_update_evidence(output))
    else:
        record.update(
            {
                "mnemosyne_update_status": "failed",
                "mnemosyne_update_urls": [],
                "mnemosyne_update_pr_numbers": [],
            }
        )
    if error:
        record["error"] = error
    record_file = state_dir / f"learn-{issue_number}.json"
    try:
        write_secure(record_file, json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning("Learn record write failed for issue #%s: %s", issue_number, exc)


def build_learn_prompt(context: str) -> str:
    """Return the standard automation prompt for the user-facing /learn skill."""
    detail = context.strip()
    suffix = f" {detail}" if detail else ""
    return (
        "/learn"
        " EXECUTE the /learn skill-creation workflow for ProjectMnemosyne."
        " Do NOT return a plan. Do NOT ask for approval."
        " Commit the results and create a PR."
        " IMPORTANT: Only push skills to the resolved ProjectMnemosyne"
        " repository (the gh user's own fork when available, else upstream),"
        " and open the PR against that same repository."
        " Do NOT create files under .claude-plugin/ in this repo."
        f"{suffix}"
    )


def _record_learn_failure(
    state_dir: Path, issue_number: int, log_file: Path, *, log_text: str, error: str
) -> None:
    """Write the FAILED: log + record for a non-recoverable ``/learn`` failure."""
    logger.warning("Learn failed for issue #%s: %s", issue_number, error)
    write_secure(log_file, log_text)
    _write_learn_record(
        state_dir,
        issue_number,
        succeeded=False,
        log_file=log_file,
        error=error,
    )


def _record_learn_success(
    state_dir: Path, issue_number: int, log_file: Path, *, stdout: str
) -> None:
    """Write the log + record for a successful ``/learn`` run."""
    write_secure(log_file, stdout)
    _write_learn_record(
        state_dir,
        issue_number,
        succeeded=True,
        log_file=log_file,
        output=stdout,
    )
    logger.info("Learn completed for issue #%s", issue_number)
    logger.info("Learn log: %s", log_file)


def _wait_for_quota_reset(reset_epoch: int, issue_number: int) -> None:
    """Block until a detected quota reset, handling the ``0`` unknown sentinel.

    ``resolve_quota_reset_epoch`` returns ``0`` when a rate-limit message is
    present but carries no parseable reset time. For that sentinel we back off a
    fixed interval rather than busy-looping on an unknown deadline; otherwise we
    wait until the concrete reset epoch.

    Args:
        reset_epoch: Reset epoch from :func:`resolve_quota_reset_epoch` (may be
            ``0`` for an unknown reset).
        issue_number: Issue number, for logging.

    """
    if reset_epoch > 0:
        logger.warning(
            "Learn rate-limited for issue #%s; waiting for reset before retry", issue_number
        )
        wait_until(reset_epoch)
    else:
        logger.warning(
            "Learn rate-limited for issue #%s with unknown reset; backing off %ds before retry",
            issue_number,
            _LEARN_UNKNOWN_RESET_BACKOFF_SECONDS,
        )
        wait_until(int(time.time()) + _LEARN_UNKNOWN_RESET_BACKOFF_SECONDS)


def run_learn(
    session_id: str,
    worktree_path: Path,
    issue_number: int,
    state_dir: Path,
    slot_id: int | None = None,
    agent: str = "claude",
    session_agent: str | None = None,
    model: str | None = None,
    *,
    timeout: int = DEFAULT_AGENT_TIMEOUT,
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
    log_file = log_file_path(state_dir, "learn", issue_number)
    if not session_agent_matches(session_agent, agent):
        message = (
            f"Session belongs to {session_agent or 'claude'}, "
            f"but selected agent is {agent}; skipping learn resume"
        )
        logger.warning("Learn skipped for issue #%s: %s", issue_number, message)
        write_secure(log_file, f"FAILED: {message}\n")
        _write_learn_record(
            state_dir,
            issue_number,
            succeeded=False,
            log_file=log_file,
            error=message,
        )
        return False

    if uses_direct_agent_runner(agent):

        def _invoke_direct_agent() -> str:
            return (
                resume_agent_session(
                    agent=agent,
                    session_id=session_id,
                    prompt=build_learn_prompt(""),
                    cwd=worktree_path,
                    timeout=timeout,
                    model=direct_agent_model(agent, "HEPH_LEARN_MODEL"),
                ).stdout
                or ""
            )

        return _run_learn_with_retry(
            _invoke_direct_agent,
            state_dir=state_dir,
            issue_number=issue_number,
            log_file=log_file,
        )

    # /learn is a SIMPLE-complexity task (summarization + file writes), so we
    # use the configured learn model (default: Haiku) but accept operator
    # overrides via HEPH_LEARN_MODEL. Callers may pass `model` explicitly to
    # run /learn on the same model tier as the session (e.g. implementer_model()).
    # We can't route through `call_claude` here because we need `--resume`
    # semantics with full Bash/Edit tools; instead we add the model flag directly.
    effective_model = model if model is not None else learn_model()
    learn_command = [
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
    ]

    def _invoke_claude() -> str:
        return run(learn_command, cwd=worktree_path, timeout=timeout).stdout or ""

    return _run_learn_with_retry(
        _invoke_claude, state_dir=state_dir, issue_number=issue_number, log_file=log_file
    )


def _run_learn_with_retry(
    invoke: Callable[[], str],
    *,
    state_dir: Path,
    issue_number: int,
    log_file: Path,
) -> bool:
    """Invoke a ``/learn`` agent command, waiting + retrying on a rate limit.

    /learn is ALWAYS attempted. When the invocation is rate-limited (e.g. right
    after a 429 session-limit) — detected via the single common resolver
    :func:`resolve_quota_reset_epoch` on the agent's stdout/stderr — we wait for
    the reset window and re-invoke the SAME command instead of dropping the
    learning (#1331). The agent CLI may signal a limit either by returning
    success with the message in stdout OR by raising with it in stdout/stderr,
    so both are inspected. A genuine non-rate-limit failure records ``FAILED:``
    and returns ``False``. The loop is bounded by
    :data:`_LEARN_RATE_LIMIT_MAX_RETRIES` so a misdetected message can't spin
    forever.

    Args:
        invoke: Zero-arg callable that runs the agent command and returns its
            stdout. Raises (with optional ``stdout``/``stderr`` attributes) on
            subprocess failure.
        state_dir: Directory for state/log files.
        issue_number: Issue number.
        log_file: Path to the ``learn-{issue}.log`` file.

    Returns:
        True if learn completed successfully, False otherwise.

    """
    for _attempt in range(_LEARN_RATE_LIMIT_MAX_RETRIES + 1):
        try:
            stdout = invoke()
        except Exception as e:  # broad catch: external agent process; non-blocking
            err_stdout = getattr(e, "stdout", "") or ""
            err_stderr = getattr(e, "stderr", "") or ""
            error_output = f"FAILED: {e}\n"
            if hasattr(e, "stdout"):
                error_output += f"\nSTDOUT:\n{err_stdout}"
            if hasattr(e, "stderr"):
                error_output += f"\nSTDERR:\n{err_stderr}"
            # A rate-limit/session-limit failure is recoverable: wait for the
            # reset and re-invoke. Only a genuine failure records FAILED:.
            reset_epoch = resolve_quota_reset_epoch(err_stderr, err_stdout, str(e))
            if reset_epoch is not None:
                write_secure(log_file, error_output)
                _wait_for_quota_reset(reset_epoch, issue_number)
                continue
            _record_learn_failure(
                state_dir, issue_number, log_file, log_text=error_output, error=str(e)
            )
            # Non-blocking: never re-raise
            return False

        # The agent CLI can exit 0 while emitting its 429/session-limit message
        # in the stdout payload. Detect it and wait/retry rather than recording
        # a bogus success.
        reset_epoch = resolve_quota_reset_epoch(stdout)
        if reset_epoch is not None:
            write_secure(log_file, stdout)
            _wait_for_quota_reset(reset_epoch, issue_number)
            continue
        _record_learn_success(state_dir, issue_number, log_file, stdout=stdout)
        return True

    # Retry budget exhausted while still rate-limited: record a failure so the
    # learn is re-run later rather than silently marked complete.
    message = f"rate-limited after {_LEARN_RATE_LIMIT_MAX_RETRIES} retries; learn not completed"
    _record_learn_failure(
        state_dir, issue_number, log_file, log_text=f"FAILED: {message}\n", error=message
    )
    return False


def learn_needs_rerun(issue_number: int, state_dir: Path) -> bool:
    """Check if learn log indicates failure.

    Args:
        issue_number: Issue number
        state_dir: Directory containing learn log files

    Returns:
        True if learn needs to be re-run (missing or failed log)

    """
    log_file = log_file_path(state_dir, "learn", issue_number)
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
    timeout: int | None = None,
    model: str | None = None,
) -> bool:
    """Send ``/compact`` to the (repo, issue, agent, model) Claude session.

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
        timeout: Subprocess timeout in seconds.

    Returns:
        True on a zero-exit subprocess call, False on any failure including
        a non-zero exit code (e.g. /compact skill not registered).

    """
    sid = session_uuid(repo, issue, agent, model)
    timeout_s = learn_claude_timeout() if timeout is None else timeout
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
            timeout=timeout_s,
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
        stderr = result.stderr or ""
        # #1587: "No conversation found with session ID" is the EXPECTED case
        # when this (agent, issue) never created a session this run (e.g. the
        # arming sweep firing /compact for agent=ci-driver on a PR the driver
        # didn't fix). There is simply nothing to compact — log at DEBUG, not
        # WARNING, so it doesn't read as a failure.
        if "No conversation found with session ID" in stderr:
            logger.debug(
                "Issue #%s: no %s session to compact (session %s); skipping",
                issue,
                agent,
                sid,
            )
            return False
        logger.warning(
            "Issue #%s: /compact for agent=%s exited %s (non-fatal); stderr=%s",
            issue,
            agent,
            result.returncode,
            stderr[:200],
        )
        return False

    logger.info("Issue #%s: /compact completed for agent=%s (session %s)", issue, agent, sid)
    return True
