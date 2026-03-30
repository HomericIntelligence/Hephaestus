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
import logging
import re
import signal
import time
from datetime import timezone

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

    now_utc = dt.datetime.now(timezone.utc)
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

    Args:
        text: Text to search (typically ``gh`` CLI stderr output).

    Returns:
        Unix timestamp when the rate limit resets, or ``None`` if no
        rate-limit message is found.

    """
    m = RATE_LIMIT_RE.search(text)
    if not m:
        return None
    return parse_reset_epoch(m.group("time"), m.group("tz"))


def wait_until(epoch: int) -> None:
    """Block until the given epoch time, printing a countdown.

    Handles ``SIGINT`` gracefully: the first interrupt prints a message
    and raises :class:`KeyboardInterrupt`.

    Args:
        epoch: Target Unix timestamp to wait for.

    Raises:
        KeyboardInterrupt: If the user presses Ctrl-C during the wait.

    """
    interrupted = False

    def handler(_sig: int, _frame: object) -> None:
        nonlocal interrupted
        interrupted = True

    old_handler = signal.signal(signal.SIGINT, handler)
    try:
        while True:
            if interrupted:
                print("\n[INFO] Wait interrupted by user")
                raise KeyboardInterrupt
            remaining = epoch - int(time.time())
            if remaining <= 0:
                print()
                return
            h, r = divmod(remaining, 3600)
            m, s = divmod(r, 60)
            print(
                f"\r[INFO] Rate limit resets in {h:02d}:{m:02d}:{s:02d}",
                end="",
                flush=True,
            )
            time.sleep(1)
    finally:
        signal.signal(signal.SIGINT, old_handler)


def detect_claude_usage_limit(stderr: str) -> bool:
    """Detect Claude API usage limit from error output.

    Args:
        stderr: Standard error output

    Returns:
        True if usage limit detected

    """
    patterns = [
        r"usage limit",
        r"quota exceeded",
        r"credit.*exhausted",
        r"billing.*limit|billing.*exceeded",  # More specific to avoid false positives
    ]

    for pattern in patterns:
        if re.search(pattern, stderr, re.IGNORECASE):
            logger.error("Claude usage limit detected: %s", pattern)
            return True

    return False
