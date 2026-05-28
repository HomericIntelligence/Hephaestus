"""Shared Claude-CLI helpers.

Verdict parsing, rate-limit detection, and deterministic-session invocation.

What lives here:

- :func:`parse_review_verdict` — verdict parser used by the strict review loops
- :func:`scan_quota_reset` — shared cross-stream rate-limit scanner so all
  phases get identical 429 handling.
- :data:`SESSION_EXPIRED_PHRASES` — substrings the Claude CLI returns when
  ``--resume`` targets a session that no longer exists locally.
- :func:`invoke_claude_with_session` — the single entry point every
  automation phase must use. Picks ``--session-id`` (first call) vs
  ``--resume`` (subsequent calls) based on whether the session's JSONL
  transcript already exists, and falls back to ``--session-id`` on any
  resume failure.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation.session_naming import session_jsonl_path, session_name
from hephaestus.github.rate_limit import detect_claude_usage_cap, detect_rate_limit

logger = logging.getLogger(__name__)


# Substrings the Claude CLI returns when ``--resume`` targets a session that
# no longer exists in local persistence.
SESSION_EXPIRED_PHRASES: tuple[str, ...] = (
    "session not found",
    "invalid session",
    "session expired",
    "no such session",
    "session does not exist",
    "cannot resume",
    "resume failed",
    "failed to resume",
)


def _session_expired(stderr: str, stdout: str) -> bool:
    """Return True if either stream indicates the resume target is gone."""
    blob = (stderr + "\n" + stdout).lower()
    return any(phrase in blob for phrase in SESSION_EXPIRED_PHRASES)


def invoke_claude_with_session(  # noqa: C901  # state machine: argv assembly + create vs resume + expired fallback toggle
    *,
    repo: str,
    issue: int | str,
    agent: str,
    githash: str,
    prompt: str,
    model: str,
    cwd: Path,
    timeout: int = 300,
    system_prompt_file: Path | None = None,
    allowed_tools: str | None = None,
    permission_mode: str | None = None,
    extra_args: list[str] | None = None,
    output_format: str = "text",
    input_via_stdin: bool = False,
    recreate_on_resume_failure: bool = True,
) -> tuple[str, str]:
    """Invoke Claude with a deterministic session.

    First call for the ``(repo, issue, agent, githash)`` tuple uses
    ``--session-id <uuid>`` to create the session. Every later call uses
    ``--resume <uuid>``. By default any ``--resume`` failure retries once
    with ``--session-id`` to recreate; quota-cap detection happens one
    layer up. Pass ``recreate_on_resume_failure=False`` to propagate the
    ``CalledProcessError`` instead — needed by callers that must apply
    their own session-expired classification (e.g. the impl review-loop
    feedback path stops iterating on expiry rather than restarting).

    Args:
        repo: Repository slug (e.g. ``"ProjectScylla"``).
        issue: Issue number — leading ``#`` is stripped by
            :func:`session_naming.session_name`.
        agent: One of the ``AGENT_*`` constants in
            :mod:`hephaestus.automation.session_naming`.
        githash: Short trunk SHA for the loop iteration.
        prompt: Prompt text. Passed as a positional argv unless
            ``input_via_stdin`` is True.
        model: ``--model`` value (use ``planner_model()`` /
            ``reviewer_model()`` / ``implementer_model()`` from
            :mod:`claude_models`).
        cwd: Working directory for the subprocess. Also determines where
            the session JSONL is probed.
        timeout: Subprocess timeout in seconds.
        system_prompt_file: Optional ``--system-prompt`` file.
        allowed_tools: Optional ``--allowedTools`` value (e.g.
            ``"Read,Glob,Grep"``).
        permission_mode: Optional ``--permission-mode`` value.
        extra_args: Any additional flags.
        output_format: ``--output-format`` (``"text"``, ``"json"``, or
            ``"stream-json"``).
        input_via_stdin: When True, ``prompt`` is fed via stdin instead of
            argv (matches the existing :mod:`plan_reviewer` invocation).
        recreate_on_resume_failure: When ``True`` (default), any
            ``--resume`` failure triggers a fresh ``--session-id`` retry.
            When ``False``, the underlying ``CalledProcessError`` is
            re-raised so the caller can distinguish expired-session from
            transient errors itself.

    Returns:
        ``(stdout, session_uuid)``. The session UUID is the deterministic
        value derived from the tuple — it equals what the CLI's
        ``--output-format=json`` would report as ``session_id``.

    Raises:
        subprocess.CalledProcessError: If a ``--session-id`` create call
            exits non-zero, or if both the ``--resume`` and the subsequent
            recreate attempt fail, or if ``--resume`` fails when
            ``recreate_on_resume_failure=False``.
        subprocess.TimeoutExpired: If the call exceeds ``timeout``.

    """
    display_name = session_name(repo, issue, agent, githash)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, display_name))
    transcript = session_jsonl_path(sid, cwd)
    should_resume = transcript.exists()

    base_tail: list[str] = ["--model", model, "--output-format", output_format]
    if system_prompt_file is not None and system_prompt_file.exists():
        base_tail += ["--system-prompt", str(system_prompt_file)]
    if allowed_tools:
        base_tail += ["--allowedTools", allowed_tools]
    if permission_mode:
        base_tail += ["--permission-mode", permission_mode]
    if extra_args:
        base_tail += extra_args
    base_tail.append("--print")
    if not input_via_stdin:
        base_tail.append(prompt)

    def _build(create: bool) -> list[str]:
        mode = ["--session-id", sid, "--name", display_name] if create else ["--resume", sid]
        return ["claude", *mode, *base_tail]

    def _run(create: bool) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        # CLAUDECODE is set by an outer Claude Code process to refuse nested
        # invocations; clear it so the automation subprocess can launch.
        env["CLAUDECODE"] = ""
        # Propagate correlation ID to subprocess if set (for gh tracing).
        from hephaestus.logging.utils import get_current_correlation_id

        cid = get_current_correlation_id()
        if cid:
            env["GH_TRACE_ID"] = cid
        cmd = _build(create)
        logger.debug(
            "claude invoke: agent=%s issue=%s sid=%s mode=%s",
            agent,
            issue,
            sid,
            "create" if create else "resume",
        )
        return subprocess.run(
            cmd,
            input=prompt if input_via_stdin else None,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
            env=env,
            stdin=subprocess.DEVNULL if not input_via_stdin else None,
            cwd=str(cwd),
        )

    if not should_resume:
        return _run(create=True).stdout, sid

    try:
        return _run(create=False).stdout, sid
    except subprocess.CalledProcessError as exc:
        if not recreate_on_resume_failure:
            raise
        # Any --resume failure falls back to a fresh session. The known
        # SESSION_EXPIRED phrases are the common case; transient failures
        # (the CLI itself crashed, a corrupted transcript, etc.) also
        # benefit from recreating rather than re-raising and losing the
        # call entirely. Quota-cap detection happens one layer up.
        expired = _session_expired(exc.stderr or "", exc.stdout or "")
        log = logger.warning if expired else logger.info
        log(
            "claude --resume %s failed (exit=%s, expired=%s); recreating session",
            sid,
            exc.returncode,
            expired,
        )
        return _run(create=True).stdout, sid


def scan_quota_reset(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Inspects each text for either form of rate-limit message — the GitHub-CLI
    "Limit reached ..." form or the Claude-CLI "out of extra usage · resets
    ..." form. ``is not None`` chaining preserves an epoch of ``0`` (rate-
    limited, reset time unknown) instead of confusing it with "no rate limit".
    """
    for text in texts:
        for detect in (detect_rate_limit, detect_claude_usage_cap):
            epoch = detect(text)
            if epoch is not None:
                return epoch
    return None


@dataclass(frozen=True)
class ReviewVerdict:
    """Parsed verdict from a review response.

    Attributes:
        grade: Letter grade extracted from ``Grade: <X>`` line. ``None`` if absent.
        verdict: One of ``"GO"``, ``"NOGO"``, or ``"AMBIGUOUS"``.
        raw: Full review text (kept for downstream prompts and logs).

    """

    grade: str | None
    verdict: str
    raw: str

    @property
    def is_go(self) -> bool:
        """True only on an unambiguous GO."""
        return self.verdict == "GO"


_GRADE_RE = re.compile(
    r"^\s*\**\s*Grade\s*:\s*\**\s*([A-F][+-]?)(?![A-Za-z])",
    re.MULTILINE | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(GO|NO[\s-]?GO)\b", re.MULTILINE | re.IGNORECASE
)


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Extract grade and Go/NoGo verdict from a review response.

    Looks for lines like:
        Grade: B+
        Verdict: GO     (or NOGO, NO-GO, NO GO)

    A response missing or contradicting these markers is treated as
    AMBIGUOUS — which the loop treats as NoGo (continue iterating).

    Args:
        text: The full review text from Claude.

    Returns:
        :class:`ReviewVerdict`.

    """
    grade_match = _GRADE_RE.search(text)
    grade = grade_match.group(1).upper() if grade_match else None

    verdict_match = _VERDICT_RE.search(text)
    if verdict_match:
        raw_verdict = re.sub(r"[\s-]", "", verdict_match.group(1).upper())
        verdict = "GO" if raw_verdict == "GO" else "NOGO"
    else:
        verdict = "AMBIGUOUS"

    return ReviewVerdict(grade=grade, verdict=verdict, raw=text)
