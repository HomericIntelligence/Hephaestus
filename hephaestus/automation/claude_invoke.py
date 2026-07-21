"""Shared Claude-CLI helpers.

Verdict parsing, rate-limit detection, and deterministic-session invocation.

What lives here:

- :func:`parse_review_verdict` — verdict parser used by automation review loops
- :func:`scan_quota_reset` — shared cross-stream rate-limit scanner so all
  phases get identical 429 handling.
- :func:`detect_model_usage_cap` — classifier for the model-specific
  "reached your <model> limit … switch models with /model" 429, which carries
  no reset epoch and is remediated by a model switch, not a wait (#1793).
- :data:`SESSION_EXPIRED_PHRASES` — substrings the Claude CLI returns when
  ``--resume`` targets a session that no longer exists locally.
- :func:`invoke_claude_with_session` — the single entry point every
  automation phase must use. Picks ``--session-id`` (first call) vs
  ``--resume`` (subsequent calls) based on whether the model-keyed JSONL
  transcript already exists. No recreate-on-failure cascade — a create/resume
  error propagates (#1168). On a model-specific usage cap it retries the same
  request once on :func:`agent_config.fallback_model` and pins the fallback
  for the rest of the process (#1793).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path

from hephaestus.automation import subprocess_registry
from hephaestus.automation.agent_config import (
    agent_default_timeout,
    fallback_model,
    resolve_session_jsonl_path,
    session_name,
)
from hephaestus.github.client import ClaudeUsageCapError, PromptTooLongError
from hephaestus.github.rate_limit import resolve_quota_reset_epoch
from hephaestus.utils.helpers import strip_null_bytes

logger = logging.getLogger(__name__)

_MAX_SUBPROCESS_FAILURE_DETAIL_CHARS = 500


def _subprocess_stream_text(value: object) -> str:
    """Return subprocess stream content as text for diagnostic logging."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


def _truncate_subprocess_detail(text: str, max_chars: int) -> str:
    """Bound subprocess diagnostics before writing them to shared logs."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "... [truncated]"


def format_called_process_error(
    exc: subprocess.CalledProcessError,
    *,
    max_chars: int = _MAX_SUBPROCESS_FAILURE_DETAIL_CHARS,
) -> str:
    """Format a ``CalledProcessError`` with bounded stdout/stderr diagnostics."""
    parts = [str(exc)]
    stderr = _subprocess_stream_text(exc.stderr).strip()
    if stderr:
        parts.append(f"stderr={_truncate_subprocess_detail(stderr, max_chars)!r}")
    stdout = _subprocess_stream_text(exc.stdout).strip()
    if stdout:
        parts.append(f"stdout={_truncate_subprocess_detail(stdout, max_chars)!r}")
    return "; ".join(parts)


def describe_claude_failure(exc: BaseException) -> str:
    """Return a bounded diagnostic string for a failed ``claude`` invocation.

    ``CalledProcessError`` carries the CLI's stderr/stdout, which is the only
    place the actual failure reason lives (#1799) — surface it via
    :func:`format_called_process_error`. Any other exception degrades to
    ``str(exc)``.
    """
    if isinstance(exc, subprocess.CalledProcessError):
        return format_called_process_error(exc)
    return str(exc)


# Substrings the Claude CLI returns when ``--resume`` targets a session that
# no longer exists in local persistence.
SESSION_EXPIRED_PHRASES: tuple[str, ...] = (
    "no conversation found",
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


# Models observed to be usage-capped in THIS process. Consulted by
# :func:`invoke_claude_with_session` so later calls skip the doomed attempt and
# go straight to the fallback (a capped model stays capped for hours; without
# stickiness an exhausted quota once fired ~39 doomed sessions in an hour).
# Deliberately per-process: phase subprocesses spawned by the loop each
# re-detect once, which costs one ~0.5s failed call per phase (#1793).
_capped_models: set[str] = set()


def is_model_capped(model: str) -> bool:
    """Return True if *model* hit a model-specific usage cap in this process."""
    return model in _capped_models


def reset_capped_models() -> None:
    """Clear the capped-model registry (test hook)."""
    _capped_models.clear()


def _record_model_cap(capped: str, fallback: str) -> None:
    """Mark *capped* as unusable for this process and log the switch."""
    _capped_models.add(capped)
    logger.warning(
        "model %s usage cap hit; falling back to %s for the rest of this run",
        capped,
        fallback,
    )


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

    Model-specific usage caps (#1793): a 429 whose message says "reached your
    <model> limit … switch models with /model" carries no reset epoch, so the
    wait-until-reset handlers cannot help. When such a cap is detected — via a
    non-zero exit OR an exit-0 ``is_error: true`` JSON envelope (json format
    only; plain-text output is never scanned, since agent prose can
    legitimately contain the phrases) — the same request is retried once on
    :func:`agent_config.fallback_model`, and the capped model is pinned to the
    fallback for the rest of this process. The fallback runs under its own
    session lineage (the model is part of the session key), so the capped
    model's cached context is re-sent once. No retry happens when the
    effective model already IS the fallback; the error propagates so callers'
    existing quota/overload handling takes over.

    Args:
        repo: Repository slug (e.g. ``"Scylla"``).
        issue: Issue number — leading ``#`` is stripped by
            :func:`session_naming.session_name`.
        agent: One of the ``AGENT_*`` constants in
            :mod:`hephaestus.automation.session_naming`.
        prompt: Prompt text. Passed as a positional argv unless
            ``input_via_stdin`` is True.
        model: ``--model`` value; also part of the session key so a session
            never crosses models.
        cwd: Working directory for the subprocess.
        timeout: Subprocess timeout in seconds. When omitted, resolves to the
            generic :func:`agent_config.agent_default_timeout` (7200s) — this
            entry point is shared by every agent type, so no phase-specific
            budget is assumed (#1415).
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
        ``(repo, issue, agent, model)`` — for the model actually used (the
        fallback's id when a cap forced a switch).

    Raises:
        subprocess.CalledProcessError: If the create/resume call exits non-zero
            (after the one fallback retry, when applicable).
        subprocess.TimeoutExpired: If the call exceeds ``timeout``.

    """
    if timeout is None:
        timeout = agent_default_timeout()
    del recreate_on_resume_failure  # back-compat shim only; no recreate cascade

    def _attempt(effective_model: str) -> tuple[str, str]:
        return _invoke_claude_once(
            repo=repo,
            issue=issue,
            agent=agent,
            prompt=prompt,
            model=effective_model,
            cwd=cwd,
            timeout=timeout,
            system_prompt_file=system_prompt_file,
            allowed_tools=allowed_tools,
            permission_mode=permission_mode,
            extra_args=extra_args,
            output_format=output_format,
            input_via_stdin=input_via_stdin,
        )

    fallback = fallback_model()
    effective = model
    if model != fallback and is_model_capped(model):
        logger.info("model %s previously capped; using fallback %s", model, fallback)
        effective = fallback
    try:
        stdout, sid = _attempt(effective)
    except subprocess.CalledProcessError as e:
        if effective == fallback or not detect_model_usage_cap(e.stderr or "", e.stdout or ""):
            raise
        _record_model_cap(effective, fallback)
        return _attempt(fallback)
    if output_format == "json" and effective != fallback:
        err_text = _envelope_error_text(stdout)
        if err_text is not None and detect_model_usage_cap(err_text):
            _record_model_cap(effective, fallback)
            return _attempt(fallback)
    return stdout, sid


def _invoke_claude_once(
    *,
    repo: str,
    issue: int | str,
    agent: str,
    prompt: str,
    model: str,
    cwd: Path,
    timeout: int,
    system_prompt_file: Path | None,
    allowed_tools: str | None,
    permission_mode: str | None,
    extra_args: list[str] | None,
    output_format: str,
    input_via_stdin: bool,
) -> tuple[str, str]:
    """Run one ``claude`` create/resume call for the given model (no fallback).

    The single-attempt body of :func:`invoke_claude_with_session`; see its
    docstring for the session-key semantics.
    """
    display_name = session_name(repo, issue, agent, model)
    sid = str(uuid.uuid5(uuid.NAMESPACE_DNS, display_name))

    # Create on FIRST use, resume after (#1168). ``claude --resume`` does NOT
    # auto-create — it errors "No conversation found" for an unknown id — so the
    # first call for a (repo, issue, agent, model) key must use ``--session-id``
    # to create the transcript under the caller's exact cwd; every later call
    # searches only this checkout's registered worktree family and ``--resume``s
    # it to reuse cached context. The probe is model-keyed because ``sid`` is.
    # This is NOT the old recreate-on-failure cascade (that mis-fired on 429s,
    # re-sending full prompts 3x and crossing models); a ``--resume`` or
    # ``--session-id`` failure simply propagates.
    transcript = resolve_session_jsonl_path(sid, cwd)
    create = not transcript.is_file()
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
    result = _run_tracked(
        cmd,
        stdin_text=prompt if input_via_stdin else None,
        timeout=timeout,
        env=env,
        use_devnull_stdin=not input_via_stdin,
        cwd=str(cwd),
    )
    return result.stdout, sid


def _run_tracked(
    cmd: list[str],
    *,
    stdin_text: str | None,
    timeout: int,
    env: dict[str, str],
    use_devnull_stdin: bool,
    cwd: str,
) -> subprocess.CompletedProcess[str]:
    """Run *cmd* like ``subprocess.run(check=True, timeout=...)`` but killable.

    Spawns the child in its OWN session (``start_new_session=True``) so it leads
    its own process group, and registers that group with
    :mod:`~hephaestus.automation.subprocess_registry` for the duration of the
    call. A pipeline teardown (``WorkerPool.shutdown``) can then ``SIGTERM`` the
    group and free the blocked worker thread instead of leaking a runaway
    ``claude`` child (#2059). On timeout the group is killed before re-raising so
    no orphan survives. The exception contract matches ``subprocess.run``:
    :class:`subprocess.CalledProcessError` on non-zero exit,
    :class:`subprocess.TimeoutExpired` on timeout.
    """
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL if use_devnull_stdin else subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=cwd,
        start_new_session=True,
    )
    with subprocess_registry.track_process_group(proc.pid):
        try:
            stdout, stderr = proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc)
            proc.communicate()  # reap so no zombie remains
            raise
        except BaseException:
            # Includes the SIGTERM-driven teardown path: never leave the child.
            _kill_process_tree(proc)
            raise
    if proc.returncode != 0:
        raise subprocess.CalledProcessError(proc.returncode, cmd, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout=stdout, stderr=stderr)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort kill of *proc*'s whole process group, falling back to the proc."""
    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
        with contextlib.suppress(ProcessLookupError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
    with contextlib.suppress(ProcessLookupError, OSError):  # pragma: no cover - already gone
        proc.kill()


def _envelope_error_text(stdout: str) -> str | None:
    """Extract the error text from an ``is_error: true`` Claude JSON envelope.

    Returns ``None`` when ``stdout`` is empty, not JSON, or not an error
    envelope; otherwise the (possibly empty) ``result`` field as a string.
    Shared by :func:`raise_for_error_envelope` and the model-cap fallback in
    :func:`invoke_claude_with_session` so the envelope decode exists once.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    if not (isinstance(data, dict) and data.get("is_error")):
        return None
    return str(data.get("result") or "")


_PROMPT_TOO_LONG_MARKER = "Prompt is too long"


def raise_for_error_envelope(stdout: str) -> None:
    """Raise if ``stdout`` is an ``is_error: true`` Claude JSON envelope.

    The Claude CLI can exit 0 while returning a JSON envelope whose
    ``is_error`` is true — e.g. a 429 quota cap, an oversized prompt, or
    another fatal API error surfaced inside the ``result`` field. Callers that
    pass ``output_format="json"`` and would otherwise treat that envelope as a
    real result (``pr_reviewer``, ``review_validator``) call this to fail
    loudly instead of forwarding the cap message downstream (#1528 follow-up).

    An oversized prompt becomes a :class:`PromptTooLongError` so callers can
    retry with a smaller diff budget instead of recording a plain ERROR
    (#1847). A quota cap becomes a :class:`ClaudeUsageCapError` carrying the
    reset epoch (a ``RuntimeError`` subclass, so existing
    ``except RuntimeError`` handlers still catch it); any other ``is_error``
    envelope becomes a plain ``RuntimeError``. Non-JSON or non-error stdout is
    left untouched.

    Args:
        stdout: The raw stdout returned by :func:`invoke_claude_with_session`.

    Raises:
        PromptTooLongError: If the envelope reports the prompt exceeded model
            context.
        ClaudeUsageCapError: If the envelope signals a 429 quota cap.
        RuntimeError: If the envelope is ``is_error`` for any other reason.

    """
    err_text = _envelope_error_text(stdout)
    if err_text is None:
        return
    if _PROMPT_TOO_LONG_MARKER in err_text:
        raise PromptTooLongError(err_text)
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


# Signals for the MODEL-SPECIFIC usage cap, e.g.:
#   "You've reached your Fable 5 limit. Run /usage-credits to continue or
#    switch models with /model."
# Unlike the session-limit / usage-cap 429s (rate_limit.py), this carries NO
# reset epoch — waiting cannot help; the remediation is a model switch. The
# negative lookahead keeps "session limit" / "usage limit" owned by the
# wait-until-reset detectors so the two families never overlap (#1793).
_MODEL_USAGE_CAP_RE = re.compile(
    r"reached\s+your\s+(?!session\b|usage\b)[\w .-]{1,40}?\s+limit",
    re.IGNORECASE,
)
_MODEL_USAGE_CAP_PHRASES: tuple[str, ...] = (
    "/usage-credits",
    "switch models with /model",
)


def detect_model_usage_cap(*texts: str) -> bool:
    """Return True if any stream carries a model-specific usage-cap message.

    Recognizes the "You've reached your <model> limit … switch models with
    /model" 429 the Claude CLI emits when one model tier's quota is exhausted
    while others remain available. Three independent signals are ORed (the
    "reached your <model> limit" regex plus each remediation-hint phrase) so a
    partial rewording still matches. Session-limit and generic usage-cap
    phrasings are deliberately NOT matched — those carry a reset epoch and stay
    with :func:`scan_quota_reset`'s wait-until-reset handling.

    Only failure streams (stderr of a non-zero exit, or the ``result`` field of
    an ``is_error: true`` JSON envelope) should be passed here — never plain
    successful output, which can legitimately mention these phrases (#1793).

    Args:
        *texts: One or more output streams to inspect (stderr and/or stdout).

    Returns:
        True if a model-specific usage-cap signal is present in any stream.

    """
    for text in texts:
        if not text:
            continue
        lowered = text.lower()
        if any(phrase in lowered for phrase in _MODEL_USAGE_CAP_PHRASES):
            return True
        if _MODEL_USAGE_CAP_RE.search(text):
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


_VERDICT_RE = re.compile(
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(CONDITIONAL[\s-]?GO|GO|NO[\s-]?GO|ERROR)\b",
    re.MULTILINE | re.IGNORECASE,
)
_SUMMARY_PAIR_RE = re.compile(
    r"^\s*\**\s*Grade\s*:\s*\**\s*(?P<grade>[A-F][+-]?)(?![A-Za-z])[ \t]*\r?\n"
    r"^\s*\**\s*Verdict\s*:\s*\**\s*(?P<verdict>CONDITIONAL[\s-]?GO|GO|NO[\s-]?GO|ERROR)\b",
    re.MULTILINE | re.IGNORECASE,
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
        Verdict: GO     (or CONDITIONAL GO, NOGO, NO-GO, NO GO, ERROR)

    A response missing a verdict is treated as AMBIGUOUS — which the loop
    treats as NoGo (continue iterating). A grade is associated only with an
    immediately adjacent preceding ``Grade`` line, so a final verdict cannot
    borrow a grade from an earlier summary. An explicit ``Verdict: ERROR`` marks
    a reviewer-infrastructure failure (see
    :data:`INFRA_ERROR_REVIEW_TEXT`) and is surfaced as ``verdict="ERROR"`` so
    callers can distinguish it from a genuine NOGO. ``CONDITIONAL GO`` is
    normalized to NOGO because the pipeline's gate is binary.

    Args:
        text: The full review text from Claude.

    Returns:
        :class:`ReviewVerdict`.

    """
    # Last verdict wins. A grade belongs to it only when it appears in the
    # immediately preceding Grade/Verdict pair; this preserves plan-review's
    # historical verdict-only format while allowing PR review to reject an
    # ungraded final verdict fail-closed.
    pairs = list(_SUMMARY_PAIR_RE.finditer(text))
    verdicts = list(_VERDICT_RE.finditer(text))
    if not verdicts:
        return ReviewVerdict(grade=None, verdict="AMBIGUOUS", raw=text)

    final_verdict = verdicts[-1]
    matching_pair = next(
        (pair for pair in reversed(pairs) if pair.start("verdict") == final_verdict.start(1)),
        None,
    )
    raw_verdict = re.sub(r"[\s-]", "", final_verdict.group(1).upper())
    verdict = "GO" if raw_verdict == "GO" else "ERROR" if raw_verdict == "ERROR" else "NOGO"
    grade = matching_pair.group("grade").upper() if matching_pair else None
    return ReviewVerdict(grade=grade, verdict=verdict, raw=text)
