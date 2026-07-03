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
  ``--resume`` (subsequent calls) based on whether the model-keyed JSONL
  transcript already exists. No recreate-on-failure cascade — a create/resume
  error propagates (#1168).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation.agent_config import (
    planner_claude_timeout,
    session_jsonl_path,
    session_name,
)
from hephaestus.github.client import ClaudeUsageCapError
from hephaestus.github.rate_limit import resolve_quota_reset_epoch
from hephaestus.utils.helpers import strip_null_bytes

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
    timeout: int | None = None,
    system_prompt_file: Path | None = None,
    allowed_tools: str | None = None,
    permission_mode: str | None = None,
    extra_args: list[str] | None = None,
    output_format: str = "text",
    input_via_stdin: bool = False,
    recreate_on_resume_failure: bool = True,  # accepted for back-compat; no longer used
) -> tuple[str, str]:
    """Invoke Claude with a deterministic per-(repo, issue, agent, model) session.

    The session id is ``uuid5`` of ``(repo, issue, agent, model)``. The FIRST
    call for a key uses ``--session-id`` to create the transcript; every later
    call ``--resume``s it so cached context is reused instead of re-sent (#1166,
    #1168). ``claude --resume`` does NOT auto-create — it errors "No conversation
    found" for an unknown id — so create-on-first-use is required; the probe is
    the model-keyed transcript file's existence. There is no expired/contention
    recreate cascade (the old one mis-fired on 429s, re-sending full prompts 3×
    and crossing models); a ``--session-id``/``--resume`` failure simply
    propagates. Because ``--resume`` is locked to the creating model, the model
    is part of the key: switching a per-agent model starts that model's own
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
        timeout: Subprocess timeout in seconds. When omitted, resolves through
            the centralized planner-agent timeout helper.
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
        subprocess.CalledProcessError: If the create/resume call exits non-zero.
        subprocess.TimeoutExpired: If the call exceeds ``timeout``.

    """
    del recreate_on_resume_failure  # back-compat shim only; no recreate cascade
    if timeout is None:
        timeout = planner_claude_timeout()

    display_name = session_name(repo, issue, agent, model)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, display_name))

    # Create on FIRST use, resume after (#1168). ``claude --resume`` does NOT
    # auto-create — it errors "No conversation found" for an unknown id — so the
    # first call for a (repo, issue, agent, model) key must use ``--session-id``
    # to create the transcript; every later call ``--resume``s it to reuse cached
    # context. The probe is the transcript file's existence, which is model-keyed
    # because ``sid`` is. This is NOT the old recreate-on-failure cascade (that
    # mis-fired on 429s, re-sending full prompts 3x and crossing models); a
    # ``--resume``/``--session-id`` failure now simply propagates.
    create = not session_jsonl_path(sid, cwd).exists()
    mode_args = ["--session-id", sid, "--name", display_name] if create else ["--resume", sid]
    cmd: list[str] = [
        "claude",
        *mode_args,
        "--model",
        model,
        "--output-format",
        output_format,
    ]
    if system_prompt_file is not None and system_prompt_file.exists():
        cmd += ["--system-prompt", str(system_prompt_file)]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if permission_mode:
        cmd += ["--permission-mode", permission_mode]
    if extra_args:
        cmd += extra_args
    # subprocess.run rejects any argv element (or text stdin) containing a NUL
    # with ``ValueError: embedded null byte``. The prompt is assembled from
    # untrusted multi-source text (issue body + agent/advise output + prior
    # review) and a single stray NUL would otherwise permanently strand the
    # issue. Strip defensively here — the one chokepoint every agent phase
    # (planner, implementer, advise, reviewer) passes through (#1661).
    sanitized = strip_null_bytes(prompt)
    if sanitized != prompt:
        logger.warning(
            "Stripped NUL byte(s) from %s prompt for issue %s before invoking claude",
            agent,
            issue,
        )
        prompt = sanitized
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

    logger.debug(
        "claude invoke: agent=%s issue=%s sid=%s mode=%s",
        agent,
        issue,
        sid,
        "create" if create else "resume",
    )
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


def raise_for_error_envelope(stdout: str) -> None:
    """Raise if ``stdout`` is an ``is_error: true`` Claude JSON envelope.

    The Claude CLI can exit 0 while returning a JSON envelope whose
    ``is_error`` is true — e.g. a 429 quota cap or another fatal API error
    surfaced inside the ``result`` field. Callers that pass
    ``output_format="json"`` and would otherwise treat that envelope as a real
    result (``pr_reviewer``, ``review_validator``) call this to fail loudly
    instead of forwarding the cap message downstream (#1528 follow-up).

    A quota cap becomes a :class:`ClaudeUsageCapError` carrying the reset epoch
    (a ``RuntimeError`` subclass, so existing ``except RuntimeError`` handlers
    still catch it); any other ``is_error`` envelope becomes a plain
    ``RuntimeError``. Non-JSON or non-error stdout is left untouched.

    Args:
        stdout: The raw stdout returned by :func:`invoke_claude_with_session`.

    Raises:
        ClaudeUsageCapError: If the envelope signals a 429 quota cap.
        RuntimeError: If the envelope is ``is_error`` for any other reason.

    """
    if not stdout:
        return
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return
    if not (isinstance(data, dict) and data.get("is_error")):
        return
    err_text = str(data.get("result") or "")
    reset_epoch = resolve_quota_reset_epoch(err_text)
    if reset_epoch is not None:
        raise ClaudeUsageCapError(
            "Claude usage cap reached",
            reset_epoch=reset_epoch if reset_epoch > 0 else None,
        )
    raise RuntimeError(f"Claude Code failed: {err_text or 'is_error=true'}")


def scan_quota_reset(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    Thin wrapper over the single common resolver
    :func:`hephaestus.github.rate_limit.resolve_quota_reset_epoch` (#1321), so
    the planner and plan-reviewer agent paths share one detection surface with
    the implementer — including the Claude session-limit 429 phrasing the older
    two-detector logic missed. ``is not None`` chaining preserves an epoch of
    ``0`` (rate-limited, reset time unknown) instead of confusing it with "no
    rate limit".
    """
    return resolve_quota_reset_epoch(*texts)


# Substrings (and a 5xx-status regex) the Claude/Anthropic API surfaces when the
# upstream model service is transiently overloaded. A ``529 Overloaded`` is a
# *server* error (the service is busy), not a quota cap — so it carries no reset
# epoch and is missed by :func:`scan_quota_reset`. It is safe to retry with
# exponential backoff. The status regex matches the documented overload statuses
# (500/502/503/504/529) without over-matching unrelated digit runs: it anchors
# on the literal "API Error" / "status" context the CLI emits (e.g.
# ``API Error: 529 Overloaded``) (#1374).
_SERVER_OVERLOAD_PHRASES: tuple[str, ...] = (
    "overloaded",
    "service unavailable",
    "internal server error",
    "bad gateway",
    "gateway timeout",
)
_SERVER_OVERLOAD_STATUS_RE = re.compile(
    r"(?:api\s+error|status(?:\s+code)?)\s*[:=]?\s*(?:5(?:00|02|03|04)|529)\b",
    re.IGNORECASE,
)


def detect_server_overload(*texts: str) -> bool:
    """Return True if any stream indicates a transient server-overload error.

    Recognizes the ``529 Overloaded`` (and generic 5xx-overload) responses the
    Claude/Anthropic API returns when the upstream model service is transiently
    busy, e.g.::

        API Error: 529 Overloaded
        529 {"type":"error","error":{"type":"overloaded_error", ...}}
        Service Unavailable (503)

    Unlike a 429 quota cap (handled by :func:`scan_quota_reset`), these carry no
    reset epoch — the correct response is a bounded exponential backoff and
    retry, not a wait-until-reset. Detection lives here so every agent-call path
    shares one classifier surface (#1374).

    Args:
        *texts: One or more output streams to inspect (stderr and/or stdout).

    Returns:
        True if a server-overload signal is present in any stream.

    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(phrase in lowered for phrase in _SERVER_OVERLOAD_PHRASES):
            return True
        if _SERVER_OVERLOAD_STATUS_RE.search(text):
            return True
    return False


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
