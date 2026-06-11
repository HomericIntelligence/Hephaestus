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

from hephaestus.automation.session_naming import session_name
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


def invoke_claude_with_session(
    *,
    repo: str,
    issue: int | str,
    agent: str,
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
    recreate_on_resume_failure: bool = True,  # accepted for back-compat; no longer used
) -> tuple[str, str]:
    """Invoke Claude with a deterministic per-(repo, issue, agent, model) session.

    The session id is ``uuid5`` of ``(repo, issue, agent, model)`` and the call
    ALWAYS uses ``--resume <uuid>`` (#1166). The Claude harness creates the
    session transparently on the first ``--resume`` for an id that does not yet
    exist, so there is no separate create path, no create-vs-resume probe, and no
    expired/contention fallback to a fresh id — every call for the same key
    continues the SAME transcript, so cached context is reused instead of
    re-sent. Because ``--resume`` is locked to the creating model, the model is
    part of the key: switching a per-agent model simply starts that model's own
    create-once-then-resume lineage rather than colliding with another model's
    transcript.

    The session is scoped to the artifact (issue/PR), not a commit SHA, so the
    transcript persists across main-bumps for the issue's lifetime (#841).

    Args:
        repo: Repository slug (e.g. ``"ProjectScylla"``).
        issue: Issue number — leading ``#`` is stripped by
            :func:`session_naming.session_name`.
        agent: One of the ``AGENT_*`` constants in
            :mod:`hephaestus.automation.session_naming`.
        prompt: Prompt text. Passed as a positional argv unless
            ``input_via_stdin`` is True.
        model: ``--model`` value; also part of the session key so a session
            never crosses models.
        cwd: Working directory for the subprocess.
        timeout: Subprocess timeout in seconds.
        system_prompt_file: Optional ``--system-prompt`` file.
        allowed_tools: Optional ``--allowedTools`` value (e.g.
            ``"Read,Glob,Grep"``).
        permission_mode: Optional ``--permission-mode`` value.
        extra_args: Any additional flags.
        output_format: ``--output-format`` (``"text"``, ``"json"``, or
            ``"stream-json"``).
        input_via_stdin: When True, ``prompt`` is fed via stdin instead of argv.
        recreate_on_resume_failure: Deprecated/ignored. Retained so existing
            keyword callers keep working; the always-resume model needs no
            recreate toggle.

    Returns:
        ``(stdout, session_uuid)`` — the deterministic id derived from
        ``(repo, issue, agent, model)``.

    Raises:
        subprocess.CalledProcessError: If the ``--resume`` call exits non-zero.
        subprocess.TimeoutExpired: If the call exceeds ``timeout``.

    """
    del recreate_on_resume_failure  # back-compat shim only; always-resume model
    display_name = session_name(repo, issue, agent, model)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, display_name))

    cmd: list[str] = ["claude", "--resume", sid, "--model", model, "--output-format", output_format]
    if system_prompt_file is not None and system_prompt_file.exists():
        cmd += ["--system-prompt", str(system_prompt_file)]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if extra_args:
        cmd += extra_args
    cmd.append("--print")
    if not input_via_stdin:
        cmd.append(prompt)

    env = os.environ.copy()
    # CLAUDECODE is set by an outer Claude Code process to refuse nested
    # invocations; clear it so the automation subprocess can launch.
    env["CLAUDECODE"] = ""
    # Propagate correlation ID to subprocess if set (for gh tracing).
    from hephaestus.logging.utils import get_current_correlation_id

    cid = get_current_correlation_id()
    if cid:
        env["GH_TRACE_ID"] = cid

    logger.debug("claude invoke: agent=%s issue=%s sid=%s mode=resume", agent, issue, sid)
    # ALWAYS --resume (#1166): the harness creates the session transparently when
    # the id does not yet exist, so there is no create path, probe, or fallback.
    # Resuming the same (repo, issue, agent, model) key reuses cached context
    # instead of re-sending the full prompt — and never crosses models, since the
    # model is part of the key.
    result = subprocess.run(
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
    return result.stdout, sid


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
        verdict: One of ``"GO"``, ``"NOGO"``, ``"AMBIGUOUS"``, or ``"ERROR"``.
        raw: Full review text (kept for downstream prompts and logs).

    ``"ERROR"`` is reserved for **reviewer-infrastructure failures** (the
    reviewer subprocess raised — API 400, timeout, crash, empty output). It is
    deliberately distinct from ``"NOGO"`` so the loop does not mistake "the
    reviewer never ran" for "the reviewer judged the code not ready": an ERROR
    must not burn toward ``state:skip`` exhaustion and must not stamp a
    go/no-go label (#911 / PR #1069).

    """

    grade: str | None
    verdict: str
    raw: str

    @property
    def is_go(self) -> bool:
        """True only on an unambiguous GO."""
        return self.verdict == "GO"

    @property
    def is_error(self) -> bool:
        """True when the verdict is a reviewer-infrastructure failure sentinel."""
        return self.verdict == "ERROR"


_GRADE_RE = re.compile(
    r"^\s*\**\s*Grade\s*:\s*\**\s*([A-F][+-]?)(?![A-Za-z])",
    re.MULTILINE | re.IGNORECASE,
)
_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(GO|NO[\s-]?GO|ERROR)\b", re.MULTILINE | re.IGNORECASE
)

# Sentinel review text emitted when the reviewer subprocess itself fails
# (e.g. an API 400 from an advisor-tier mismatch, a timeout, or a crash). It
# parses to ``verdict="ERROR"`` via :func:`parse_review_verdict`, which the
# review loop treats as inconclusive — re-review next loop, never skip/label.
INFRA_ERROR_REVIEW_TEXT = "Grade: F\nVerdict: ERROR\n"


def parse_review_verdict(text: str) -> ReviewVerdict:
    """Extract grade and Go/NoGo verdict from a review response.

    Looks for lines like:
        Grade: B+
        Verdict: GO     (or NOGO, NO-GO, NO GO, ERROR)

    A response missing or contradicting these markers is treated as
    AMBIGUOUS — which the loop treats as NoGo (continue iterating). An explicit
    ``Verdict: ERROR`` marks a reviewer-infrastructure failure (see
    :data:`INFRA_ERROR_REVIEW_TEXT`) and is surfaced as ``verdict="ERROR"`` so
    callers can distinguish it from a genuine NOGO.

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
        if raw_verdict == "GO":
            verdict = "GO"
        elif raw_verdict == "ERROR":
            verdict = "ERROR"
        else:
            verdict = "NOGO"
    else:
        verdict = "AMBIGUOUS"

    return ReviewVerdict(grade=grade, verdict=verdict, raw=text)
