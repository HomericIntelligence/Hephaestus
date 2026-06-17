"""GitHub rate-limit detection and wait utilities.

Parses GitHub CLI rate-limit messages and provides blocking waits
with countdown display.  All functions use only the standard library.

Usage:
    from hephaestus.github.rate_limit import detect_rate_limit, wait_until

    epoch = detect_rate_limit(gh_stderr_output)
    if epoch is not None:
        wait_until(epoch)
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover – Python 3.8 backport
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# Regex matching GitHub CLI rate-limit messages, e.g.:
#   "Limit reached ... resets 2:30pm (America/Los_Angeles)"
RATE_LIMIT_RE = re.compile(
    r"Limit reached.*resets\s+(?P<time>[0-9:apm]+)\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)

# Regex matching GitHub GraphQL rate-limit messages, e.g.:
#   "GraphQL: API rate limit exceeded for user ID 4211002"
#   "GraphQL: API rate limit already exceeded for user ID 4211002"
# Also matches the bare phrase inside JSON error payloads.
GRAPHQL_RATE_LIMIT_RE = re.compile(
    r"(?:GraphQL:\s*)?API rate limit (?:already )?exceeded",
    re.IGNORECASE,
)

# Regex matching GitHub secondary rate-limit messages, e.g.:
#   "You have exceeded a secondary rate limit. Please wait a few minutes before you try again."
SECONDARY_RATE_LIMIT_RE = re.compile(
    r"exceeded a secondary rate limit",
    re.IGNORECASE,
)


def detect_secondary_rate_limit(text: str) -> bool:
    """Return True if *text* contains a GitHub secondary rate-limit message.

    Secondary rate limits differ from primary ones: they carry no reset epoch
    and are triggered by request frequency or concurrency, not hourly quotas.

    Args:
        text: Text to search (typically ``gh`` CLI stderr or stdout).

    Returns:
        True if a secondary rate-limit message is detected.

    """
    return bool(SECONDARY_RATE_LIMIT_RE.search(text))


# Regex matching Claude CLI usage-cap messages, e.g.:
#   "You're out of extra usage · resets May 8, 5pm (America/Los_Angeles)"
#   "Claude usage limit reached · resets 9pm (America/Los_Angeles)"
# The date portion is optional; when missing, parse_reset_epoch falls back
# to today/tomorrow logic.
CLAUDE_USAGE_CAP_RE = re.compile(
    r"resets\s+(?:(?P<date>[A-Za-z]+\s+\d{1,2})\s*,?\s+)?"
    r"(?P<time>\d{1,2}(?::\d{2})?(?:am|pm)?)\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)

# Regex matching Claude CLI session-limit messages, e.g.:
#   "You've hit your session limit · resets 4:20am"
#   "You've hit your session limit · resets 9pm (America/Los_Angeles)"
# Unlike CLAUDE_USAGE_CAP_RE, the parenthesized timezone is OPTIONAL here:
# the session-limit phrasing emitted by ``claude -p`` on a 429 frequently
# omits it (#1321). The optional ``resets <time>`` is captured when present;
# when the whole "resets ..." clause is absent the message still matches so
# callers can fall back to a probe / unknown-reset sentinel.
CLAUDE_SESSION_LIMIT_RE = re.compile(
    r"(?:hit your |reached your |you'?ve hit your |)session limit"
    r"(?:.*?resets\s+(?P<time>\d{1,2}(?::\d{2})?(?:am|pm)?)"
    r"\s*(?:\((?P<tz>[^)]+)\))?)?",
    re.IGNORECASE,
)

# Months for parsing date-prefixed usage-cap messages
_MONTHS = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

ALLOWED_TIMEZONES: set[str] = {
    "America/Los_Angeles",
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "Asia/Tokyo",
}

# Internal constants for AM/PM conversion
_NOON_HOUR = 12
_MIDNIGHT_HOUR = 0


def parse_reset_epoch(time_str: str, tz: str) -> int:
    """Parse a rate-limit reset time string and return epoch seconds.

    Args:
        time_str: Time string like ``"2pm"``, ``"2:30pm"``, or ``"14:00"``
        tz: IANA timezone string like ``"America/Los_Angeles"``.
            Falls back to ``"America/Los_Angeles"`` if not in
            :data:`ALLOWED_TIMEZONES`.

    Returns:
        Unix timestamp (epoch seconds) when the rate limit resets.
        If *time_str* cannot be parsed, returns ``now + 3600`` as a
        safe fallback.

    """
    if tz not in ALLOWED_TIMEZONES:
        tz = "America/Los_Angeles"

    now_utc = dt.datetime.now(dt.timezone.utc)
    today = now_utc.astimezone(ZoneInfo(tz)).date()

    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", time_str, re.IGNORECASE)
    if not m:
        return int(time.time()) + 3600

    hour, minute, ampm = m.groups()
    hour = int(hour)
    minute = int(minute or 0)

    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < _NOON_HOUR:
            hour += _NOON_HOUR
        if ampm == "am" and hour == _NOON_HOUR:
            hour = _MIDNIGHT_HOUR

    local = dt.datetime.combine(
        today,
        dt.time(hour, minute),
        tzinfo=ZoneInfo(tz),
    )

    if local < now_utc.astimezone(ZoneInfo(tz)):
        local += dt.timedelta(days=1)

    return int(local.timestamp())


def detect_rate_limit(text: str) -> int | None:
    """Detect a rate-limit message in *text* and return the reset epoch.

    Recognises two phrasings:

    * The ``gh`` CLI REST limit message that includes a reset time and
      timezone, e.g. ``"Limit reached ... resets 2:30pm (America/Los_Angeles)"``.
    * The GraphQL limit message, e.g. ``"GraphQL: API rate limit exceeded
      for user ID NNNN"`` — this form does not embed a reset time, so the
      function falls back to :func:`gh_rate_limit_reset_epoch` (a one-shot
      ``gh api rate_limit`` probe with a short cache). When that probe
      cannot answer, ``0`` is returned as a sentinel meaning "rate limited
      with unknown reset" — callers should interpret it as a request to
      back off without a fixed deadline.

    Args:
        text: Text to search (typically ``gh`` CLI stderr output, or a
            GraphQL JSON error payload rendered as a string).

    Returns:
        Unix timestamp when the rate limit resets, ``0`` if rate-limited
        with unknown reset, or ``None`` if no rate-limit message is found.

    """
    m = RATE_LIMIT_RE.search(text)
    if m:
        return parse_reset_epoch(m.group("time"), m.group("tz"))
    if GRAPHQL_RATE_LIMIT_RE.search(text):
        probed = gh_rate_limit_reset_epoch()
        return probed if probed is not None else 0
    return None


# Cached ``gh api rate_limit`` probe. Keyed by () — the cache holds the
# most recent (epoch, fetched_at_monotonic) tuple. The TTL is short
# because the reset window itself only updates hourly, but we still
# revalidate every ~30s so that after a wait the next caller doesn't
# act on a stale "0 remaining" view.
_RATE_LIMIT_PROBE_TTL = 30.0
_rate_limit_probe_cache: dict[str, tuple[int | None, float]] = {}


def gh_rate_limit_reset_epoch(resource: str = "graphql") -> int | None:
    """Return the upcoming reset epoch for a GitHub API resource, or ``None``.

    Calls ``gh api rate_limit`` to fetch the current rate-limit window for
    *resource* (one of ``"graphql"``, ``"core"``, ``"search"``, …). Results
    are cached for :data:`_RATE_LIMIT_PROBE_TTL` seconds to avoid recursive
    probe storms when rate-limit detection is happening in tight loops.

    Args:
        resource: Resource name as it appears under ``.resources`` in the
            ``gh api rate_limit`` JSON. Defaults to ``"graphql"`` since
            that is what hephaestus's hot paths consume.

    Returns:
        Unix timestamp when the resource's window resets, or ``None`` if
        the probe failed (gh missing, auth missing, network error, etc.).

    """
    cached = _rate_limit_probe_cache.get(resource)
    now = time.monotonic()
    if cached is not None and (now - cached[1]) < _RATE_LIMIT_PROBE_TTL:
        return cached[0]

    try:
        result = subprocess.run(
            ["gh", "api", "rate_limit"],
            capture_output=True,
            check=True,
            text=True,
            timeout=10,
        )
        payload = json.loads(result.stdout)
        reset_val = payload.get("resources", {}).get(resource, {}).get("reset")
        epoch = int(reset_val) if reset_val is not None else None
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError, OSError) as e:
        logger.debug("gh api rate_limit probe failed: %s", e)
        epoch = None

    _rate_limit_probe_cache[resource] = (epoch, now)
    return epoch


# ---------------------------------------------------------------------------
# Cross-process token-bucket throttle
#
# The per-thread throttle in github_api.py only paces a single Python
# process. When run_automation_loop.sh fans out 3 repos × 3 planner
# workers, we get up to 9 independent processes hammering ``gh`` at
# their own per-thread cap. The bucket below is shared via a flock'd
# state file so all callers compose into a single global rate budget.
# ---------------------------------------------------------------------------

_DEFAULT_GLOBAL_RATE = 10.0  # tokens per second
_DEFAULT_BURST = 30.0  # max tokens stored
_global_throttle_rate = _DEFAULT_GLOBAL_RATE
_global_throttle_burst = _DEFAULT_BURST


def configure_gh_global_throttle(rate: float, burst: float) -> None:
    """Configure the process-local global ``gh`` throttle parameters.

    CLI entrypoints call this before making GitHub requests. The state file is
    still shared across processes; the rate/burst values are deliberately
    process-local so operators configure them explicitly via CLI flags instead
    of ambient environment variables.

    Args:
        rate: Tokens per second. ``0`` disables the throttle.
        burst: Maximum stored tokens. Must be at least ``1.0``.

    Raises:
        ValueError: If either value is non-finite, if rate is negative, or if
            burst is less than ``1.0``.

    """
    if not math.isfinite(rate) or rate < 0:
        raise ValueError(f"rate must be a finite non-negative number, got {rate!r}")
    if not math.isfinite(burst) or burst < 1.0:
        raise ValueError(f"burst must be a finite number >= 1.0, got {burst!r}")

    global _global_throttle_rate, _global_throttle_burst
    _global_throttle_rate = float(rate)
    _global_throttle_burst = float(burst)


def _current_uid_fragment() -> str:
    """Return a stable user identifier for fallback runtime paths."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows path
        return "user"
    return str(getuid())


def _owned_by_current_user(path: Path) -> bool:
    """Return whether ``path`` is owned by the current user when POSIX uid exists."""
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # pragma: no cover - Windows path
        return True
    try:
        return path.stat().st_uid == int(getuid())
    except OSError:
        return False


def _runtime_base_dir() -> Path:
    """Return the preferred secure root for user-specific runtime artifacts.

    All transient state lives under ``TMPDIR`` so callers have one root to
    control. A per-user component avoids sharing the same lock/state directory
    across users when ``TMPDIR`` falls back to ``/tmp``.
    """
    tmpdir = os.environ.get("TMPDIR") or "/tmp"  # nosec B108: explicit fallback root
    return Path(tmpdir) / f"hephaestus-{_current_uid_fragment()}"


def _ensure_private_dir(path: Path) -> None:
    """Create ``path`` as a current-user private directory, rejecting symlinks."""
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked runtime directory: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Refusing to use unsafe runtime directory: {path}")
    if not _owned_by_current_user(path):
        raise RuntimeError(f"Refusing to use runtime directory not owned by current user: {path}")
    path.chmod(0o700)


def _global_throttle_state_path() -> Path:
    return _runtime_base_dir() / "gh-rate" / "hephaestus_gh_rate.json"


def _open_secure_state_file(path: Path) -> TextIO:
    """Open the throttle state file without following symlinks."""
    _ensure_private_dir(path.parent.parent)
    _ensure_private_dir(path.parent)
    if path.exists() and path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked throttle state file: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "r+")


def gh_global_throttle_acquire() -> None:
    """Block until one token from the global ``gh`` rate budget is available.

    The bucket is shared across all processes on this machine via a small
    JSON state file guarded by ``fcntl.flock``. Refill rate defaults to
    ``10`` tokens/sec with a burst of ``30``; CLI entrypoints can override
    both through :func:`configure_gh_global_throttle`. Setting the rate to
    ``0`` disables the throttle entirely (useful for tests and for callers
    that already hold a known budget).

    On platforms without ``fcntl`` (Windows) the throttle silently no-ops;
    the per-thread throttle in :mod:`hephaestus.automation.github_api`
    still applies.
    """
    rate = _global_throttle_rate
    if rate <= 0:
        return
    burst = _global_throttle_burst

    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows path
        return

    state_path = _global_throttle_state_path()

    # Loop because the bucket may be empty when we first acquire the lock;
    # we sleep for the time required to refill one token, then retry.
    while True:
        with _open_secure_state_file(state_path) as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                fh.seek(0)
                raw = fh.read()
                try:
                    state = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    state = {}

                tokens = float(state.get("tokens", burst))
                updated = float(state.get("updated", 0.0))
                now = time.monotonic()
                if updated <= 0.0:
                    updated = now

                tokens = min(burst, tokens + (now - updated) * rate)
                wait = 0.0
                if tokens >= 1.0:
                    tokens -= 1.0
                else:
                    wait = (1.0 - tokens) / rate
                    # Don't deduct when waiting — we'll re-acquire and try
                    # again with a refilled budget.

                fh.seek(0)
                fh.truncate()
                json.dump({"tokens": tokens, "updated": now}, fh)
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        if wait <= 0.0:
            return
        time.sleep(wait)


def _parse_reset_with_date(date_str: str, time_str: str, tz: str) -> int:
    """Parse a date+time string like ``"May 8", "5pm", "America/Los_Angeles"``.

    Args:
        date_str: Date string like ``"May 8"`` (month name + day).
        time_str: Time string like ``"5pm"`` or ``"14:30"`` — same form
            ``parse_reset_epoch`` accepts.
        tz: IANA timezone, falls back to ``America/Los_Angeles``.

    Returns:
        Unix timestamp for the parsed datetime, or ``now + 3600`` on failure.

    """
    if tz not in ALLOWED_TIMEZONES:
        tz = "America/Los_Angeles"

    dm = re.match(r"^([A-Za-z]+)\s+(\d{1,2})$", date_str.strip())
    if not dm:
        # Date didn't parse — fall back to today/tomorrow logic
        return parse_reset_epoch(time_str, tz)

    month_name, day_str = dm.groups()
    month = _MONTHS.get(month_name[:3].lower())
    if month is None:
        return parse_reset_epoch(time_str, tz)
    day = int(day_str)

    tm = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)?$", time_str, re.IGNORECASE)
    if not tm:
        return int(time.time()) + 3600

    hour, minute, ampm = tm.groups()
    hour = int(hour)
    minute = int(minute or 0)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour < _NOON_HOUR:
            hour += _NOON_HOUR
        if ampm == "am" and hour == _NOON_HOUR:
            hour = _MIDNIGHT_HOUR

    now_local = dt.datetime.now(dt.timezone.utc).astimezone(ZoneInfo(tz))
    # Year disambiguation: if the parsed month/day is more than 6 months in the
    # past, assume next year (handles end-of-year wrap).
    year = now_local.year
    try:
        candidate = dt.datetime(year, month, day, hour, minute, tzinfo=ZoneInfo(tz))
    except ValueError:
        return int(time.time()) + 3600

    if candidate < now_local - dt.timedelta(days=180):
        candidate = candidate.replace(year=year + 1)

    return int(candidate.timestamp())


def detect_claude_usage_cap(text: str) -> int | None:
    """Detect a Claude CLI usage-cap message and return the reset epoch.

    Recognizes messages produced by ``claude -p`` when its API quota is
    exhausted, e.g.::

        You're out of extra usage · resets May 8, 5pm (America/Los_Angeles)
        Claude usage limit reached · resets 9pm (America/Los_Angeles)

    These can appear in either the CLI's stderr OR (more often) inside the
    ``stdout`` JSON payload as the ``result`` field of an ``is_error: true``
    response. Callers should pass both streams.

    Args:
        text: Text to search (CLI stderr or stdout).

    Returns:
        Unix timestamp when the cap resets, or ``None`` if no usage-cap
        message is found.

    """
    m = CLAUDE_USAGE_CAP_RE.search(text)
    if not m:
        return None
    date_str = m.group("date")
    time_str = m.group("time")
    tz = m.group("tz")
    if date_str:
        return _parse_reset_with_date(date_str, time_str, tz)
    return parse_reset_epoch(time_str, tz)


def detect_session_limit(text: str) -> int | None:
    """Detect a Claude CLI session-limit message and return the reset epoch.

    Recognizes the 429 phrasing ``claude -p`` emits when the per-session quota
    is exhausted, e.g.::

        You've hit your session limit · resets 4:20am

    Crucially this form often omits the parenthesized timezone that
    :func:`detect_claude_usage_cap` requires, so that detector misses it and
    the orchestrator hard-fails instead of waiting (#1321). When a ``resets
    <time>`` clause is present the epoch is parsed (defaulting the timezone via
    :func:`parse_reset_epoch`'s ``America/Los_Angeles`` fallback and its
    today/tomorrow logic). When the message matches but carries no reset time,
    ``0`` is returned as the "rate-limited, reset unknown" sentinel so callers
    back off rather than treating it as "no limit".

    Args:
        text: Text to search (CLI stderr, or the stdout JSON ``result`` field
            of an ``is_error: true`` response).

    Returns:
        Unix timestamp when the session limit resets, ``0`` if a session-limit
        message is present without a parseable reset time, or ``None`` if no
        session-limit message is found.

    """
    m = CLAUDE_SESSION_LIMIT_RE.search(text)
    if not m:
        return None
    time_str = m.group("time")
    if not time_str:
        return 0
    # tz is optional; parse_reset_epoch falls back to America/Los_Angeles when
    # tz is empty/unknown, and to tomorrow when the time is already past.
    return parse_reset_epoch(time_str, m.group("tz") or "")


def resolve_quota_reset_epoch(*texts: str) -> int | None:
    """Find a quota-reset epoch across one or more output streams.

    This is the **single common resolver** for every agent-invocation path
    (implement, review, plan, follow-up/``/learn``). It runs all quota
    detectors so a phrasing gap is fixed once here rather than in each call
    site (#1321):

    * :func:`detect_rate_limit` — ``gh`` REST / GraphQL limit messages.
    * :func:`detect_claude_usage_cap` — Claude usage-cap messages (with tz).
    * :func:`detect_session_limit` — Claude session-limit 429s (tz optional).

    ``is not None`` chaining preserves an epoch of ``0`` (rate-limited, reset
    unknown) instead of confusing it with "no rate limit".

    Args:
        *texts: One or more output streams to inspect (stderr and/or stdout).

    Returns:
        The first reset epoch found (possibly ``0`` for unknown-reset), or
        ``None`` if no quota message is present in any stream.

    """
    for text in texts:
        if not text:
            continue
        for detect in (detect_rate_limit, detect_claude_usage_cap, detect_session_limit):
            epoch = detect(text)
            if epoch is not None:
                return epoch
    return None


# Non-TTY progress is throttled to this interval (seconds) so a redirected
# log file gets one line per 10 minutes instead of one line per 1Hz tick.
_NON_TTY_EMIT_INTERVAL_S = 600


def _countdown_loop(epoch: int, is_interrupted: Callable[[], bool]) -> None:
    r"""Display a countdown until ``epoch``, or until ``is_interrupted`` is true.

    On an interactive terminal (``sys.stdout.isatty()``), a 1Hz ``\\r``
    spinner is printed. When stdout is redirected to a non-TTY (e.g. a log
    file), the spinner is suppressed and a progress line is logged at most
    once every 10 minutes to avoid flooding the log with one line per tick.

    Args:
        epoch: Target Unix timestamp to wait for.
        is_interrupted: Callable returning True when the wait should abort.

    Raises:
        KeyboardInterrupt: If ``is_interrupted`` becomes true during the wait.

    """
    # Defensive safety: if ``time.sleep`` returns instantly (e.g. because a
    # test has monkeypatched it), the print-driven countdown loop below
    # would otherwise busy-spin and OOM the process. Watch monotonic_ns
    # across iterations and bail out if too many iterations occur in too
    # little wall-clock time.
    start_mono = time.monotonic_ns()
    iterations = 0
    iteration_cap = 100_000  # ~27hrs at 1Hz; well above any real countdown

    is_tty = sys.stdout.isatty()
    # Track last non-TTY emission with the monotonic clock; ``time.time`` is
    # heavily mocked in tests, so monotonic keeps the throttle honest. ``None``
    # forces an emission on the first iteration so even a short wait logs once.
    last_emit_mono: float | None = None

    while True:
        if is_interrupted():
            print("\n[INFO] Wait interrupted by user")
            raise KeyboardInterrupt
        remaining = epoch - int(time.time())
        if remaining <= 0:
            if is_tty:
                print()
            return
        h, r = divmod(remaining, 3600)
        m, s = divmod(r, 60)
        if is_tty:
            print(
                f"\r[INFO] Rate limit resets in {h:02d}:{m:02d}:{s:02d}",
                end="",
                flush=True,
            )
        else:
            now_mono = time.monotonic()
            if last_emit_mono is None or now_mono - last_emit_mono >= _NON_TTY_EMIT_INTERVAL_S:
                logger.info("Rate limit resets in %02d:%02d:%02d", h, m, s)
                last_emit_mono = now_mono
        time.sleep(1)
        iterations += 1
        if iterations >= iteration_cap:
            # Either the system clock is broken or sleep is mocked;
            # either way, stop spinning and let the caller proceed.
            elapsed_s = (time.monotonic_ns() - start_mono) / 1e9
            logger.warning("wait_until iteration cap reached after %.2fs; bailing out", elapsed_s)
            if is_tty:
                print()
            return


def wait_until(epoch: int) -> None:
    """Block until the given epoch time, printing a countdown.

    On the main thread, ``SIGINT`` is handled gracefully: the first interrupt
    prints a message and raises :class:`KeyboardInterrupt`. When called from a
    worker thread, the custom handler is skipped — ``signal.signal`` may only be
    called from the main thread — and Ctrl-C still propagates normally via the
    interpreter's default handling on the main thread.

    Args:
        epoch: Target Unix timestamp to wait for.

    Raises:
        KeyboardInterrupt: If the user presses Ctrl-C during the wait
            (main-thread invocations only).

    """
    if threading.current_thread() is not threading.main_thread():
        # signal.signal() raises ValueError off the main thread. Run the
        # countdown without a custom handler.
        _countdown_loop(epoch, lambda: False)
        return

    interrupted = False

    def handler(_sig: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    old_handler = signal.signal(signal.SIGINT, handler)
    try:
        _countdown_loop(epoch, lambda: interrupted)
    finally:
        signal.signal(signal.SIGINT, old_handler)


def detect_claude_usage_limit(stderr: str) -> bool:
    """Detect Claude API usage limit from error output.

    Only matches Claude-specific usage-limit messages, not GitHub's own
    "API usage limit" messages.  The patterns are ordered from most-specific
    to least-specific.

    Args:
        stderr: Standard error output

    Returns:
        True if usage limit detected

    """
    patterns = [
        # Claude-specific usage-limit phrasing (A5-01: tightened to avoid
        # false-triggering on GitHub's own "API usage limit" messages)
        r"Claude.*usage limit",
        r"out of extra usage",
        r"claude\.com/upgrade",
        r"quota exceeded",
        r"credit.*exhausted",
        r"billing.*limit|billing.*exceeded",  # More specific to avoid false positives
    ]

    for pattern in patterns:
        if re.search(pattern, stderr, re.IGNORECASE):
            logger.error("Claude usage limit detected: %s", pattern)
            return True

    return False
