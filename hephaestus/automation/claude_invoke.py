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
import time
import uuid
from collections.abc import Callable
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


def _run_fresh_session(
    run_session: Callable[..., subprocess.CompletedProcess[str]],
    display_name: str,
    contended_sid: str,
) -> tuple[str, str]:
    """Create and run a brand-new uuid4 session, decoupled from a contended id.

    Both the create path and the resume path fall back here when the
    deterministic uuid5 session id is unrecoverable (another worker holds it).
    uuid4 guarantees no collision with the deterministic id or another worker's
    fresh one, so the call proceeds instead of aborting.

    Args:
        run_session: The ``_run`` closure from :func:`invoke_claude_with_session`.
        display_name: Base session name; the fresh id's prefix is appended.
        contended_sid: The deterministic id that could not be used (for logs).

    Returns:
        ``(stdout, fresh_sid)`` from the fresh session.

    """
    fresh_sid = str(uuid.uuid4())
    fresh_name = f"{display_name}-{fresh_sid[:8]}"
    logger.warning(
        "claude session %s unrecoverable; creating fresh session %s",
        contended_sid,
        fresh_sid,
    )
    return (
        run_session(create=True, sid_override=fresh_sid, name_override=fresh_name).stdout,
        fresh_sid,
    )


def invoke_claude_with_session(  # noqa: C901  # state machine: argv assembly + create vs resume + expired fallback toggle
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
    recreate_on_resume_failure: bool = True,
) -> tuple[str, str]:
    """Invoke Claude with a deterministic session.

    First call for the ``(repo, issue, agent)`` tuple uses
    ``--session-id <uuid>`` to create the session. Every later call uses
    ``--resume <uuid>``. The session is scoped to the artifact (issue/PR),
    not to a commit SHA, so the transcript persists across main-bumps for
    the entire lifetime of the issue (#841). By default any ``--resume``
    failure retries once with ``--session-id`` to recreate; quota-cap
    detection happens one layer up. Pass ``recreate_on_resume_failure=False``
    to propagate the ``CalledProcessError`` instead — needed by callers
    that must apply their own session-expired classification (e.g. the
    impl review-loop feedback path stops iterating on expiry rather than
    restarting).

    Args:
        repo: Repository slug (e.g. ``"ProjectScylla"``).
        issue: Issue number — leading ``#`` is stripped by
            :func:`session_naming.session_name`.
        agent: One of the ``AGENT_*`` constants in
            :mod:`hephaestus.automation.session_naming`.
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
    display_name = session_name(repo, issue, agent)
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

    def _build(
        create: bool, sid_override: str | None = None, name_override: str | None = None
    ) -> list[str]:
        use_sid = sid_override or sid
        use_name = name_override or display_name
        mode = ["--session-id", use_sid, "--name", use_name] if create else ["--resume", use_sid]
        return ["claude", *mode, *base_tail]

    def _run(
        create: bool, sid_override: str | None = None, name_override: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        # CLAUDECODE is set by an outer Claude Code process to refuse nested
        # invocations; clear it so the automation subprocess can launch.
        env["CLAUDECODE"] = ""
        # Propagate correlation ID to subprocess if set (for gh tracing).
        from hephaestus.logging.utils import get_current_correlation_id

        cid = get_current_correlation_id()
        if cid:
            env["GH_TRACE_ID"] = cid
        cmd = _build(create, sid_override=sid_override, name_override=name_override)
        logger.debug(
            "claude invoke: agent=%s issue=%s sid=%s mode=%s",
            agent,
            issue,
            sid_override or sid,
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
        try:
            return _run(create=True).stdout, sid
        except subprocess.CalledProcessError as exc:
            # Defense in depth (#822): the probe says no transcript exists
            # but the CLI says the session is already registered. Encoding
            # drift between hephaestus and the CLI can desync the probe;
            # treat the rejection as proof the session exists and resume it.
            stderr = (exc.stderr or "") + (exc.stdout or "")
            if "already in use" not in stderr.lower():
                raise
            # Under concurrency (3 parallel CI-fix workers) two workers can race
            # on the same deterministic UUIDv5 session before the transcript is
            # on disk; the loser hits "already in use" (observed: ProjectHermes
            # #647). The session may still be initializing in the sibling worker,
            # so resume can ALSO fail. Retry resume with backoff; if it never
            # frees, fall back to a FRESH unique session so the drive proceeds
            # instead of aborting the PR.
            logger.warning(
                "claude --session-id %s rejected as already in use; resuming instead",
                sid,
            )
            for resume_attempt in range(3):
                try:
                    return _run(create=False).stdout, sid
                except subprocess.CalledProcessError as resume_exc:
                    logger.warning(
                        "claude --resume %s failed (attempt %s/3): %s",
                        sid,
                        resume_attempt + 1,
                        ((resume_exc.stderr or "") + (resume_exc.stdout or ""))[:200],
                    )
                    time.sleep(2 * (resume_attempt + 1))
            # Resume never succeeded — derive a fresh, unique session so this
            # worker is not coupled to the contended id.
            return _run_fresh_session(_run, display_name, sid)

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
        try:
            return _run(create=True).stdout, sid
        except subprocess.CalledProcessError as recreate_exc:
            # The recreate reuses the deterministic sid, so under concurrency a
            # sibling worker that just created this session makes the recreate
            # collide with "already in use" too (observed: planner ProjectHermes).
            # Without this guard the error propagated as "Session ID … is already
            # in use" and aborted the call. Fall back to a fresh unique session,
            # mirroring the should_resume=False path above.
            recreate_err = (recreate_exc.stderr or "") + (recreate_exc.stdout or "")
            if "already in use" not in recreate_err.lower():
                raise
            return _run_fresh_session(_run, display_name, sid)


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
