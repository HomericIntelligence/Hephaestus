"""GitHub rate-limit detection and wait utilities.

Parses GitHub CLI rate-limit messages and provides blocking waits
with countdown display. Subprocess probes route through Hephaestus's
shared helper for consistent timeout handling.

Usage:
    from hephaestus.git.gh.rate_limit import detect_rate_limit, wait_until

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
from concurrent.futures import CancelledError
from functools import cache, lru_cache
from pathlib import Path
from typing import TextIO
from zoneinfo import ZoneInfo

from hephaestus.cli.localization import text
from hephaestus.git.gh.environment import gh_child_environment
from hephaestus.utils.helpers import run_subprocess

logger = logging.getLogger(__name__)

# Darwin's confstr name is not exposed in Python's os.confstr_names. The
# integer value comes from <unistd.h> and selects the per-user temporary dir.
_CS_DARWIN_USER_TEMP_DIR = 65537


class OperationDeadlineExpired(subprocess.TimeoutExpired):
    """Report an exhausted GitHub request deadline without a fabricated duration."""

    def __init__(self, operation: str, *, next_step: str = "GitHub request") -> None:
        """Keep TimeoutExpired classification and name the step that could not start."""
        self.next_step = next_step
        super().__init__(operation, 0)

    def __str__(self) -> str:
        """Describe the deadline state instead of a zero-second child process."""
        return f"{self.cmd}: absolute monotonic deadline expired before the next {self.next_step}"


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

    now_utc = dt.datetime.now(dt.UTC)
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


def detect_rate_limit(text: str, *, allow_probe: bool = True) -> int | None:
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
        allow_probe: If false, use only the message and a recent cached reset.
            Return ``0`` when the reset time is unknown. Do not start a request.

    Returns:
        Unix timestamp when the rate limit resets, ``0`` if rate-limited
        with unknown reset, or ``None`` if no rate-limit message is found.

    """
    m = RATE_LIMIT_RE.search(text)
    if m:
        return parse_reset_epoch(m.group("time"), m.group("tz"))
    if GRAPHQL_RATE_LIMIT_RE.search(text):
        probed = gh_rate_limit_reset_epoch(allow_probe=allow_probe)
        return probed if probed is not None else 0
    return None


# Cached ``gh api rate_limit`` probe. One entry holds the pair
# ``(remaining, reset_epoch, fetched_at_monotonic)`` so a caller never mixes a
# value from one probe with a value from another window. The TTL is short
# because the reset window itself only updates hourly, but we still revalidate
# every ~30s so that after a wait the next caller does not act on a stale
# "0 remaining" view.
_RATE_LIMIT_PROBE_TTL = 30.0
_PROBE_TIMEOUT = 10.0
_rate_limit_probe_cache: dict[str, tuple[int | None, int | None, float]] = {}
# One probe in flight at a time. A burst of threads must not start one
# subprocess each when the cache is cold.
_rate_limit_probe_lock = threading.Lock()


def _probe_rate_limit_payload(
    *, timeout: float = _PROBE_TIMEOUT, shutdown: threading.Event | None = None
) -> dict[str, object] | None:
    """Run ``gh api rate_limit`` and return the parsed payload, or ``None``.

    The probe endpoint does not consume the account budget. One refresh per
    :data:`_RATE_LIMIT_PROBE_TTL` is therefore cheap.

    Args:
        timeout: Child process timeout in seconds.
        shutdown: Optional cancellation event for the child process.

    """
    try:
        result = run_subprocess(
            ["gh", "api", "rate_limit"],
            check=True,
            timeout=timeout,
            log_on_error=False,
            env=gh_child_environment(),
            shutdown=shutdown,
        )
        payload = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, ValueError, OSError) as e:
        logger.debug("gh api rate_limit probe failed: %s", e)
        return None
    return payload if isinstance(payload, dict) else None


def _resource_facts(payload: object, resource: str) -> dict[str, object]:
    """Return the ``resources.<resource>`` record from a rate-limit payload."""
    if not isinstance(payload, dict):
        return {}
    resources = payload.get("resources")
    if not isinstance(resources, dict):
        return {}
    record = resources.get(resource)
    return record if isinstance(record, dict) else {}


def _non_negative_int(value: object) -> int | None:
    """Return *value* as a non-negative integer, or ``None`` when invalid."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def gh_rate_limit_reset_epoch(resource: str = "graphql", *, allow_probe: bool = True) -> int | None:
    """Return the upcoming reset epoch for a GitHub API resource, or ``None``.

    Calls ``gh api rate_limit`` to fetch the current rate-limit window for
    *resource* (one of ``"graphql"``, ``"core"``, ``"search"``, …). Results
    are cached for :data:`_RATE_LIMIT_PROBE_TTL` seconds to avoid recursive
    probe storms when rate-limit detection is happening in tight loops.

    Args:
        resource: Resource name as it appears under ``.resources`` in the
            ``gh api rate_limit`` JSON. Defaults to ``"graphql"`` since
            that is what hephaestus's hot paths consume.
        allow_probe: If false, return only a recent cached result. Do not start
            a request when the cache is absent or expired.

    Returns:
        Unix timestamp when the resource's window resets, or ``None`` if
        the probe failed (gh missing, auth missing, network error, etc.).

    """
    cached = _rate_limit_probe_cache.get(resource)
    now = time.monotonic()
    if cached is not None and (now - cached[2]) < _RATE_LIMIT_PROBE_TTL:
        return cached[1]
    if not allow_probe:
        return None

    _, reset_epoch = _rate_limit_facts(resource)
    return reset_epoch


def _rate_limit_facts(
    resource: str,
    *,
    allow_probe: bool = True,
    deadline_s: float | None = None,
    shutdown: threading.Event | None = None,
) -> tuple[int | None, int | None]:
    """Return ``(remaining, reset_epoch)`` from one cached or fresh probe.

    A fresh probe holds :data:`_rate_limit_probe_lock` so that concurrent
    callers share one request. The child process is bounded by
    :data:`_PROBE_TIMEOUT` and by the operation deadline, and the caller
    re-checks cancellation after the probe before any outcome is cached.

    Args:
        resource: Resource name under ``.resources`` in the probe payload.
        allow_probe: If false, return only a recent cached result.
        deadline_s: Absolute monotonic deadline for the operation.
        shutdown: Optional cancellation event.

    """
    cached = _rate_limit_probe_cache.get(resource)
    now = time.monotonic()
    if cached is not None and (now - cached[2]) < _RATE_LIMIT_PROBE_TTL:
        return cached[0], cached[1]
    if not allow_probe:
        return None, None

    budget = _throttle_remaining(deadline_s, shutdown)
    lock_timeout = _PROBE_TIMEOUT if budget is None else min(_PROBE_TIMEOUT, budget)
    if not _rate_limit_probe_lock.acquire(timeout=lock_timeout):
        _throttle_remaining(deadline_s, shutdown)
        return None, None
    try:
        cached = _rate_limit_probe_cache.get(resource)
        now = time.monotonic()
        if cached is not None and (now - cached[2]) < _RATE_LIMIT_PROBE_TTL:
            return cached[0], cached[1]

        budget = _throttle_remaining(deadline_s, shutdown)
        timeout = _PROBE_TIMEOUT if budget is None else min(_PROBE_TIMEOUT, budget)
        payload = _probe_rate_limit_payload(timeout=timeout, shutdown=shutdown)
        # A cancelled or expired probe must go to the caller. Do not cache an
        # outcome that only reflects the interruption.
        _throttle_remaining(deadline_s, shutdown)
        record = _resource_facts(payload, resource)
        remaining = _non_negative_int(record.get("remaining"))
        reset_epoch = _non_negative_int(record.get("reset"))
        _rate_limit_probe_cache[resource] = (remaining, reset_epoch, time.monotonic())
        return remaining, reset_epoch
    finally:
        _rate_limit_probe_lock.release()


def gh_rate_limit_budget(
    resource: str = "graphql",
    *,
    allow_probe: bool = True,
    deadline_s: float | None = None,
    shutdown: threading.Event | None = None,
) -> tuple[int | None, int | None]:
    """Return ``(remaining, reset_epoch)`` for a GitHub API resource.

    This is the quota view that lets a caller wait for the end of the window
    instead of guessing a delay. Both values come from one cached
    ``gh api rate_limit`` probe, so they always describe the same window.

    Args:
        resource: Resource name as it appears under ``.resources`` in the
            ``gh api rate_limit`` JSON.
        allow_probe: If false, return only a recent cached result. Do not start
            a request when the cache is absent or expired.
        deadline_s: Absolute monotonic deadline for the probe.
        shutdown: Optional cancellation event for the probe.

    Returns:
        A ``(remaining, reset_epoch)`` tuple. Each member is ``None`` when the
        probe failed or the payload is malformed. Both members are ``None``
        when no recent cached result is available.

    """
    return _rate_limit_facts(
        resource,
        allow_probe=allow_probe,
        deadline_s=deadline_s,
        shutdown=shutdown,
    )


# ---------------------------------------------------------------------------
# Cross-process token-bucket throttle
#
# The per-thread throttle in github_api.py only paces a single Python
# process. When run_automation_loop.sh fans out 3 repos × 3 planner
# workers, we get up to 9 independent processes hammering ``gh`` at
# their own per-thread cap. The bucket below is shared through a state
# file under an operating-system runtime directory. The scope is one
# machine. A nonblocking ``flock`` poll loop serializes processes on that
# machine. The state root never comes from an agent-wide temporary
# directory, so temporary-file sharing cannot put the lock on a shared
# filesystem.
# ---------------------------------------------------------------------------

# GitHub meters 5,000 GraphQL points and 5,000 REST requests per hour for an
# ordinary user. A token-bucket rate above that ceiling can never engage: it
# permits more requests than the quota allows, so callers hit the limit while
# the bucket still reports tokens available. Pace below the quota instead.
#
# 5,000 per hour is about 1.39 per second. The default leaves headroom so a
# concurrent loop, a CI check, or an operator command on the same account does
# not exhaust the shared budget.
_GH_HOURLY_QUOTA = 5_000.0
_DEFAULT_GLOBAL_RATE = _GH_HOURLY_QUOTA / 3600.0 * 0.5  # about 0.69 tokens/second
_DEFAULT_BURST = 30.0  # max tokens stored

# Reserve of GraphQL points. At or below this number of remaining points, the
# throttle waits for the hourly window to reset.
_BUDGET_RESERVE = 100

# Wait a little past the reset so the new window is visible to the next probe.
_BUDGET_WAIT_MARGIN = 5.0

# Longest budget wait that the throttle accepts. One GitHub window is one
# hour. A reset further away than this is implausible: it can come from a
# corrupted payload, a clock error, or a value in the wrong unit. In that
# case the throttle falls back to the token bucket instead of sleeping.
_BUDGET_MAX_WAIT = 3_900.0

# The automation loop spends GraphQL points, so that is the budget it watches.
_BUDGET_RESOURCE = "graphql"

#: Public alias for CLI argument defaults.
DEFAULT_GLOBAL_RATE = _DEFAULT_GLOBAL_RATE

#: Scope of the global throttle budget. The state root is node-local, so the
#: budget applies to one machine and does not coordinate separate hosts.
GLOBAL_THROTTLE_SCOPE = "machine"

_global_throttle_rate = _DEFAULT_GLOBAL_RATE
_global_throttle_burst = _DEFAULT_BURST
_global_throttle_directory: Path | None = None


def configure_gh_global_throttle(
    rate: float,
    burst: float,
    *,
    directory: str | os.PathLike[str] | None = None,
) -> None:
    """Configure the process-local global ``gh`` throttle parameters.

    CLI entrypoints call this before making GitHub requests. The state file is
    still shared across processes on this machine; the rate/burst values are
    deliberately process-local so operators configure them explicitly via CLI
    flags instead of ambient environment variables.

    Args:
        rate: Tokens per second. ``0`` disables the throttle.
        burst: Maximum stored tokens. Must be at least ``1.0``.
        directory: Optional absolute parent directory for the state file. The
            function appends a per-user component and ``gh-rate``. ``None``
            uses the operating-system runtime root.

    Raises:
        ValueError: If either value is non-finite, if rate is negative, if
            burst is less than ``1.0``, or if directory is relative.

    """
    if not math.isfinite(rate) or rate < 0:
        raise ValueError(f"rate must be a finite non-negative number, got {rate!r}")
    if not math.isfinite(burst) or burst < 1.0:
        raise ValueError(f"burst must be a finite number >= 1.0, got {burst!r}")

    resolved_directory: Path | None = None
    if directory is not None:
        candidate = Path(directory).expanduser()
        if not candidate.is_absolute():
            raise ValueError(f"directory must be an absolute path, got {directory!r}")
        resolved_directory = candidate

    global _global_throttle_rate, _global_throttle_burst, _global_throttle_directory
    _global_throttle_rate = float(rate)
    _global_throttle_burst = float(burst)
    _global_throttle_directory = resolved_directory


def _current_uid_fragment() -> str:
    """Return a stable user identifier for fallback runtime paths."""
    return str(os.getuid())


def _owned_by_current_user(path: Path) -> bool:
    """Return whether ``path`` is owned by the current POSIX user."""
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def _darwin_user_temp_dir() -> Path | None:
    """Return the macOS per-user temporary directory, or ``None``."""
    try:
        value = os.confstr(_CS_DARWIN_USER_TEMP_DIR)
    except (ValueError, OSError) as exc:
        logger.debug("CS_DARWIN_USER_TEMP_DIR is unavailable: %s", exc)
        return None
    return Path(value) if value else None


def _node_local_runtime_candidates() -> list[Path]:
    """Return node-local runtime roots in preference order.

    The candidates come from operating-system runtime locations. This function
    does not read an agent-wide temporary-directory environment variable.
    Thus, temporary-file sharing cannot put the throttle state on a shared
    filesystem.
    """
    if sys.platform == "darwin":
        candidate = _darwin_user_temp_dir()
        return [] if candidate is None else [candidate]

    candidates: list[Path] = []
    xdg_runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime_dir:
        xdg_path = Path(xdg_runtime_dir)
        if xdg_path.is_absolute():
            candidates.append(xdg_path)
    candidates.append(Path("/run/user") / _current_uid_fragment())
    candidates.append(Path("/dev/shm"))  # nosec B108: node-local tmpfs runtime root
    return candidates


def _probe_private_runtime_root(root: Path) -> tuple[Path | None, str | None]:
    """Create the private per-user root below *root*, or report why it fails."""
    base = root / f"hephaestus-{_current_uid_fragment()}"
    try:
        _ensure_private_dir(base)
    except (OSError, RuntimeError) as exc:
        return None, str(exc)
    return base, None


@cache
def _log_runtime_base_once(base: Path) -> None:
    """Report the resolved throttle root one time for each base."""
    logger.debug("GitHub throttle scope=%s root=%s", GLOBAL_THROTTLE_SCOPE, base)


def _runtime_base_dir() -> Path:
    """Return a node-local private root for the GitHub throttle state.

    An explicit configured directory has precedence. Otherwise, the function
    probes the operating-system runtime candidates in order. The budget scope
    is one machine. This function never uses an agent-wide temporary directory.
    """
    configured = _global_throttle_directory
    if configured is not None:
        base, reason = _probe_private_runtime_root(configured)
        if base is None:
            raise RuntimeError(
                f"Cannot use the configured GitHub throttle directory {configured}: "
                f"{reason}. Pass --gh-throttle-dir with a writable node-local directory."
            )
        _log_runtime_base_once(base)
        return base

    failures: list[str] = []
    for candidate in _node_local_runtime_candidates():
        base, reason = _probe_private_runtime_root(candidate)
        if base is not None:
            _log_runtime_base_once(base)
            return base
        failures.append(f"{candidate}: {reason}")
    detail = "; ".join(failures) if failures else "no candidate is available"
    raise RuntimeError(
        "No node-local runtime directory is available for the GitHub throttle "
        f"({detail}). Pass --gh-throttle-dir with a writable node-local directory."
    )


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


def _load_persisted_throttle_number(value: object) -> float | None:
    """Return a finite persisted number or ``None`` when the value is invalid."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number


def _load_persisted_throttle_state(raw: str, *, burst: float, now: float) -> tuple[float, float]:
    """Return persisted throttle state, or recover conservatively from corruption.

    Empty files behave like a new full bucket. Non-empty malformed payloads
    recover as a depleted bucket at ``now`` so the next wait stays bounded.
    """
    if not raw:
        return burst, 0.0

    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return 0.0, now

    if not isinstance(state, dict):
        return 0.0, now

    tokens = _load_persisted_throttle_number(state.get("tokens"))
    updated = _load_persisted_throttle_number(state.get("updated"))
    if tokens is None or updated is None:
        return 0.0, now
    if tokens < 0.0 or tokens > burst:
        return 0.0, now
    if updated < 0.0 or updated > now:
        return 0.0, now
    return tokens, updated


def _throttle_remaining(deadline_s: float | None, shutdown: threading.Event | None) -> float | None:
    """Check cancellation and return the remaining throttle wait budget."""
    if shutdown is not None and shutdown.is_set():
        raise CancelledError("GitHub throttle was cancelled")
    if deadline_s is None:
        return None
    budget = deadline_s - time.monotonic()
    if budget <= 0:
        raise OperationDeadlineExpired("GitHub throttle deadline", next_step="GitHub request")
    return budget


def _throttle_pause(
    seconds: float, deadline_s: float | None, shutdown: threading.Event | None
) -> None:
    """Wait only within the existing throttle operation budget."""
    budget = _throttle_remaining(deadline_s, shutdown)
    delay = seconds if budget is None else min(seconds, budget)
    if shutdown is None:
        time.sleep(delay)
    else:
        shutdown.wait(delay)
    _throttle_remaining(deadline_s, shutdown)


def _invalidate_budget_cache() -> None:
    """Drop the cached budget view after a wait or an elapsed reset."""
    _rate_limit_probe_cache.pop(_BUDGET_RESOURCE, None)


@lru_cache(maxsize=8)
def _warn_budget_reset(reset_epoch: int, past: bool) -> None:
    """Warn one time for each unusual reset report.

    The value can be in the wrong unit or outside the datetime range. A warning
    must not fail the call, so an unformattable value is shown as a number.
    """
    try:
        reset_text = dt.datetime.fromtimestamp(reset_epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        reset_text = str(reset_epoch)
    if past:
        logger.warning(
            "GitHub %s budget is low and the reported reset at %s is in the "
            "past. Using the token bucket. Check the host clock.",
            _BUDGET_RESOURCE,
            reset_text,
        )
        return
    logger.warning(
        "GitHub %s budget reports a reset at %s, beyond the %.0f-second "
        "limit. Using the token bucket instead. Check the host clock.",
        _BUDGET_RESOURCE,
        reset_text,
        _BUDGET_MAX_WAIT,
    )


def _wait_for_budget(*, deadline_s: float | None, shutdown: threading.Event | None) -> bool:
    """Wait for the quota window to reset when the GitHub budget is low.

    A healthy budget returns immediately. A low budget waits for the reset
    that GitHub reports, so the caller spends the last points only after the
    new window opens. An unavailable or unusable budget returns immediately
    and lets the token bucket pace the caller.

    Returns:
        True when the probe reported a usable budget. The caller then has no
        reason to consult the token bucket. False when the budget is unknown,
        past, or implausible.

    """
    remaining, reset_epoch = gh_rate_limit_budget(
        _BUDGET_RESOURCE, deadline_s=deadline_s, shutdown=shutdown
    )
    if remaining is None or reset_epoch is None:
        return False
    if remaining > _BUDGET_RESERVE:
        return True
    delay = reset_epoch - time.time() + _BUDGET_WAIT_MARGIN
    if delay <= 0:
        # A low budget with a reset in the past is not healthy. The payload is
        # stale or the host clock is ahead. The token bucket is the safe path.
        _warn_budget_reset(reset_epoch, past=True)
        return False
    if delay > _BUDGET_MAX_WAIT:
        _warn_budget_reset(reset_epoch, past=False)
        return False

    reset_text = dt.datetime.fromtimestamp(reset_epoch).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(
        "GitHub %s budget is low: %d remaining, reserve %d. "
        "Waiting until the reset at %s (about %.0f seconds).",
        _BUDGET_RESOURCE,
        remaining,
        _BUDGET_RESERVE,
        reset_text,
        delay,
    )
    _throttle_pause(delay, deadline_s, shutdown)
    _invalidate_budget_cache()
    return True


def gh_global_throttle_acquire(
    *, deadline_s: float | None = None, shutdown: threading.Event | None = None
) -> None:
    """Block until the GitHub quota admits one more request.

    The throttle first reads the account budget from the cached
    ``gh api rate_limit`` probe. A healthy budget returns immediately. A
    budget at or below the reserve waits for the reset that GitHub reports,
    then returns. An unknown budget falls back to the token bucket, which is
    shared across all processes in one operating-system instance via a small
    JSON state file under a node-local runtime directory. The file
    is guarded by a nonblocking ``fcntl.flock`` poll loop, so the lock never
    waits inside the storage client. The budget scope is one machine. The
    state root never comes from an agent-wide temporary directory; an operator
    can select a different node-local root with ``--gh-throttle-dir``. Refill
    rate defaults to half the 5,000/hour account quota (about ``0.69``
    tokens/sec) with a burst of ``30``; CLI entrypoints can override both
    through :func:`configure_gh_global_throttle`. Setting the rate to ``0``
    disables the throttle entirely (useful for tests and for callers that
    already hold a known budget).

    An enabled throttle requires ``fcntl`` when the budget is unknown. A
    healthy budget returns before the import. Otherwise, if the primitive is
    unavailable, its import error propagates before token acquisition.
    """
    if deadline_s is not None and (
        isinstance(deadline_s, bool) or not math.isfinite(deadline_s) or deadline_s <= 0
    ):
        raise ValueError("deadline_s must be a finite positive monotonic time")
    _throttle_remaining(deadline_s, shutdown)
    rate = _global_throttle_rate
    if rate <= 0:
        return
    if _wait_for_budget(deadline_s=deadline_s, shutdown=shutdown):
        # The probe can consume part of the operation budget. Re-check before
        # the caller starts its request.
        _throttle_remaining(deadline_s, shutdown)
        return
    burst = _global_throttle_burst

    import fcntl

    state_path = _global_throttle_state_path()

    # Loop because the bucket may be empty when we first acquire the lock;
    # we sleep for the time required to refill one token, then retry.
    while True:
        _throttle_remaining(deadline_s, shutdown)
        with _open_secure_state_file(state_path) as fh:
            # Poll a nonblocking lock. A blocking flock on a shared filesystem
            # can wait without a bound inside the storage client. The lock is
            # on this inode; do not publish the state with os.replace, or two
            # processes can lock different inodes.
            while True:
                _throttle_remaining(deadline_s, shutdown)
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _throttle_pause(0.05, deadline_s, shutdown)
            try:
                _throttle_remaining(deadline_s, shutdown)
                fh.seek(0)
                raw = fh.read()
                now = time.monotonic()
                tokens, updated = _load_persisted_throttle_state(raw, burst=burst, now=now)
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
                json.dump({"tokens": float(tokens), "updated": float(now)}, fh, allow_nan=False)
                fh.flush()
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

        if wait <= 0.0:
            return
        _throttle_pause(wait, deadline_s, shutdown)


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

    now_local = dt.datetime.now(dt.UTC).astimezone(ZoneInfo(tz))
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
    for output_text in texts:
        if not output_text:
            continue
        for detect in (detect_rate_limit, detect_claude_usage_cap, detect_session_limit):
            epoch = detect(output_text)
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
            print(text("\n[INFO] Wait interrupted by user"))
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
                text(
                    "\r[INFO] Rate limit resets in %(value0)02d:%(value1)02d:%(value2)02d",
                    value0=h,
                    value1=m,
                    value2=s,
                ),
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
