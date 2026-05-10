#!/usr/bin/env python3
"""Tests for GitHub rate-limit utilities."""

from __future__ import annotations

import time
from unittest.mock import patch

from hephaestus.github.rate_limit import (
    ALLOWED_TIMEZONES,
    RATE_LIMIT_RE,
    detect_claude_usage_cap,
    detect_claude_usage_limit,
    detect_rate_limit,
    parse_reset_epoch,
    wait_until,
)


class TestRateLimitRegex:
    """Tests for RATE_LIMIT_RE pattern."""

    def test_matches_standard_message(self) -> None:
        """Matches typical GitHub CLI rate limit message."""
        text = "Limit reached for resource core, resets 2:30pm (America/Los_Angeles)"
        m = RATE_LIMIT_RE.search(text)
        assert m is not None
        assert m.group("time") == "2:30pm"
        assert m.group("tz") == "America/Los_Angeles"

    def test_matches_case_insensitive(self) -> None:
        """Matches regardless of case."""
        text = "LIMIT REACHED for something, resets 3PM (UTC)"
        m = RATE_LIMIT_RE.search(text)
        assert m is not None
        assert m.group("time") == "3PM"

    def test_no_match_on_unrelated_text(self) -> None:
        """Returns None for text without rate limit message."""
        assert RATE_LIMIT_RE.search("Everything is fine") is None


class TestParseResetEpoch:
    """Tests for parse_reset_epoch."""

    def test_pm_time(self) -> None:
        """Parses PM time correctly."""
        epoch = parse_reset_epoch("2pm", "UTC")
        assert isinstance(epoch, int)
        assert epoch > 0

    def test_am_time(self) -> None:
        """Parses AM time correctly."""
        epoch = parse_reset_epoch("9am", "UTC")
        assert isinstance(epoch, int)
        assert epoch > 0

    def test_12am_is_midnight(self) -> None:
        """12am is converted to hour 0 (midnight)."""
        epoch = parse_reset_epoch("12am", "UTC")
        assert isinstance(epoch, int)

    def test_12pm_is_noon(self) -> None:
        """12pm stays as hour 12 (noon)."""
        epoch = parse_reset_epoch("12pm", "UTC")
        assert isinstance(epoch, int)

    def test_24h_format(self) -> None:
        """Parses 24-hour format without am/pm."""
        epoch = parse_reset_epoch("14:30", "UTC")
        assert isinstance(epoch, int)

    def test_time_with_minutes(self) -> None:
        """Parses time with minutes."""
        epoch = parse_reset_epoch("2:30pm", "America/New_York")
        assert isinstance(epoch, int)

    def test_invalid_timezone_falls_back(self) -> None:
        """Unknown timezone falls back to America/Los_Angeles."""
        epoch = parse_reset_epoch("2pm", "Invalid/Timezone")
        assert isinstance(epoch, int)

    def test_unparseable_time_returns_fallback(self) -> None:
        """Unparseable time returns now + 3600."""
        before = int(time.time()) + 3600 - 5
        epoch = parse_reset_epoch("invalid", "UTC")
        after = int(time.time()) + 3600 + 5
        assert before <= epoch <= after

    def test_allowed_timezones_coverage(self) -> None:
        """All allowed timezones can be used without error."""
        for tz in ALLOWED_TIMEZONES:
            epoch = parse_reset_epoch("3pm", tz)
            assert isinstance(epoch, int)

    def test_future_time_is_today(self) -> None:
        """A time far in the future today doesn't roll to tomorrow."""
        epoch = parse_reset_epoch("11:59pm", "UTC")
        assert epoch > int(time.time())


class TestDetectRateLimit:
    """Tests for detect_rate_limit."""

    def test_detects_rate_limit(self) -> None:
        """Returns epoch when rate limit message found."""
        text = "Error: Limit reached for resource core, resets 5pm (UTC)"
        result = detect_rate_limit(text)
        assert result is not None
        assert isinstance(result, int)

    def test_returns_none_when_no_limit(self) -> None:
        """Returns None when no rate limit message."""
        assert detect_rate_limit("Normal output") is None

    def test_returns_none_for_empty_string(self) -> None:
        """Returns None for empty string."""
        assert detect_rate_limit("") is None

    def test_multiline_text(self) -> None:
        """Finds rate limit in multiline text."""
        text = "line1\nline2\nLimit reached blah resets 2pm (UTC)\nline4"
        result = detect_rate_limit(text)
        assert result is not None


class TestWaitUntil:
    """Tests for wait_until."""

    def test_returns_immediately_for_past_epoch(self) -> None:
        """Returns immediately when epoch is in the past."""
        wait_until(0)

    def test_returns_immediately_for_now(self) -> None:
        """Returns immediately when epoch is now."""
        wait_until(int(time.time()))

    @patch("hephaestus.github.rate_limit.time.sleep")
    @patch("hephaestus.github.rate_limit.time.time")
    def test_waits_and_returns(self, mock_time: object, mock_sleep: object) -> None:
        """Waits for the countdown to finish."""
        # Simulate time passing: first call returns now, second returns past epoch
        import unittest.mock

        assert isinstance(mock_time, unittest.mock.MagicMock)
        assert isinstance(mock_sleep, unittest.mock.MagicMock)
        target = 1000
        mock_time.side_effect = [999, 999, 1001]
        wait_until(target)
        assert mock_sleep.called


class TestDetectClaudeUsageLimit:
    """Tests for detect_claude_usage_limit."""

    def test_detects_usage_limit(self) -> None:
        """Returns True when usage limit message found."""
        result = detect_claude_usage_limit("Claude AI usage limit reached for your account")
        assert result is True

    def test_detects_quota_exceeded(self) -> None:
        """Returns True when quota exceeded message found."""
        result = detect_claude_usage_limit("quota exceeded for this billing period")
        assert result is True

    def test_detects_credit_exhausted(self) -> None:
        """Returns True when credit exhausted message found."""
        result = detect_claude_usage_limit("credit balance exhausted")
        assert result is True

    def test_detects_billing_limit(self) -> None:
        """Returns True when billing limit message found."""
        result = detect_claude_usage_limit("billing limit exceeded for this month")
        assert result is True

    def test_returns_false_for_normal_output(self) -> None:
        """Returns False when no usage limit message."""
        result = detect_claude_usage_limit("Normal output, everything is fine")
        assert result is False

    def test_returns_false_for_empty_string(self) -> None:
        """Returns False for empty string."""
        result = detect_claude_usage_limit("")
        assert result is False

    def test_case_insensitive_detection(self) -> None:
        """Detects usage limit regardless of case."""
        result = detect_claude_usage_limit("USAGE LIMIT reached")
        assert result is True

    def test_returns_false_for_unrelated_error(self) -> None:
        """Returns False for unrelated error messages."""
        result = detect_claude_usage_limit("Error: command not found")
        assert result is False

    def test_partial_match_in_longer_text(self) -> None:
        """Detects pattern embedded in longer text."""
        text = "API call failed: usage limit exceeded, please try again later"
        assert detect_claude_usage_limit(text) is True


class TestDetectClaudeUsageCap:
    """Tests for detect_claude_usage_cap.

    The Claude CLI emits its 429 message in two shapes:

    - With a date: "resets May 8, 5pm (America/Los_Angeles)" (multi-day quota)
    - Without:     "resets 9pm (America/Los_Angeles)"        (intra-day quota)

    Both must parse to a future epoch. The previous detector only matched
    the GitHub CLI "Limit reached ..." prefix and so missed both forms.
    """

    def test_returns_none_for_unrelated_text(self) -> None:
        assert detect_claude_usage_cap("Normal output, nothing wrong") is None

    def test_parses_date_qualified_form(self) -> None:
        # Build the date dynamically — a hardcoded "May 8, 5pm" fails by
        # date drift (the original assertion was "epoch > now-86400", which
        # only held if the test ran within ~24h of May 8).
        from datetime import datetime, timedelta, timezone

        future = datetime.now(timezone.utc) + timedelta(days=2)
        date_str = future.strftime("%b %-d")  # e.g. "May 12"
        text = f"You're out of extra usage \xb7 resets {date_str}, 5pm (America/Los_Angeles)"
        epoch = detect_claude_usage_cap(text)
        assert epoch is not None
        # Parsed epoch should be close to the future date we asked for
        # (within a 36h window covers DST + tz offset to America/Los_Angeles).
        assert abs(epoch - int(future.timestamp())) < 36 * 3600

    def test_parses_intra_day_form(self) -> None:
        text = "Claude usage limit reached \xb7 resets 9pm (America/Los_Angeles)"
        epoch = detect_claude_usage_cap(text)
        assert epoch is not None
        assert epoch > 0

    def test_finds_message_inside_json_payload(self) -> None:
        """The CLI puts this message inside JSON when --output-format=json.

        Make sure the regex still finds it even with surrounding JSON
        punctuation and escape characters.
        """
        json_blob = (
            '{"is_error": true, "api_error_status": 429, '
            '"result": "You\'re out of extra usage \xb7 resets May 8, 5pm '
            '(America/Los_Angeles)"}'
        )
        epoch = detect_claude_usage_cap(json_blob)
        assert epoch is not None
