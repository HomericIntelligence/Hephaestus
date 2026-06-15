#!/usr/bin/env python3
"""Tests for GitHub rate-limit utilities."""

from __future__ import annotations

import time
from unittest.mock import patch

from hephaestus.github.rate_limit import (
    ALLOWED_TIMEZONES,
    GRAPHQL_RATE_LIMIT_RE,
    RATE_LIMIT_RE,
    SECONDARY_RATE_LIMIT_RE,
    _countdown_loop,
    _rate_limit_probe_cache,
    detect_claude_usage_cap,
    detect_claude_usage_limit,
    detect_rate_limit,
    detect_secondary_rate_limit,
    detect_session_limit,
    gh_global_throttle_acquire,
    gh_rate_limit_reset_epoch,
    parse_reset_epoch,
    resolve_quota_reset_epoch,
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


class TestSecondaryRateLimitRegex:
    """Tests for SECONDARY_RATE_LIMIT_RE and detect_secondary_rate_limit."""

    def test_matches_github_secondary_message(self) -> None:
        """Matches the exact GitHub secondary rate-limit message."""
        text = (
            "gh: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again."
        )
        assert SECONDARY_RATE_LIMIT_RE.search(text) is not None

    def test_matches_case_insensitive(self) -> None:
        """Match is case-insensitive."""
        assert SECONDARY_RATE_LIMIT_RE.search("Exceeded A Secondary Rate Limit") is not None

    def test_no_match_on_primary_rate_limit(self) -> None:
        """Does not match primary rate-limit messages."""
        assert SECONDARY_RATE_LIMIT_RE.search("API rate limit exceeded") is None

    def test_no_match_on_unrelated_text(self) -> None:
        """Returns None for unrelated text."""
        assert SECONDARY_RATE_LIMIT_RE.search("Everything is fine") is None

    def test_detect_secondary_rate_limit_true(self) -> None:
        """detect_secondary_rate_limit returns True for the exact GH message."""
        text = (
            "gh: You have exceeded a secondary rate limit. "
            "Please wait a few minutes before you try again. "
            "For more on scraping GitHub and how it may affect your rights, "
            "please review our Terms of Service"
        )
        assert detect_secondary_rate_limit(text) is True

    def test_detect_secondary_rate_limit_false_for_primary(self) -> None:
        """detect_secondary_rate_limit returns False for primary rate-limit text."""
        assert detect_secondary_rate_limit("API rate limit exceeded for user ID 42") is False

    def test_detect_secondary_rate_limit_false_for_empty(self) -> None:
        """detect_secondary_rate_limit returns False for empty string."""
        assert detect_secondary_rate_limit("") is False


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

    def test_runs_on_worker_thread_without_crashing(self) -> None:
        """wait_until() must not raise when called off the main thread.

        Regression for #441: signal.signal() raises ValueError on non-main
        threads. wait_until() is reached from ThreadPoolExecutor workers during
        parallel automation, so it must skip the SIGINT handler off-thread.
        """
        import threading

        error: list[BaseException] = []

        def worker() -> None:
            try:
                wait_until(0)  # past epoch — returns immediately
            except BaseException as exc:  # capture for assertion in parent thread
                error.append(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert error == [], f"wait_until raised on worker thread: {error}"


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
        """Detects Claude-specific usage limit regardless of case (A5-01).

        The tightened pattern requires "Claude" before "usage limit" to avoid
        false-triggering on GitHub's own "API usage limit" messages.
        """
        result = detect_claude_usage_limit("CLAUDE usage limit reached")
        assert result is True

    def test_returns_false_for_unrelated_error(self) -> None:
        """Returns False for unrelated error messages."""
        result = detect_claude_usage_limit("Error: command not found")
        assert result is False

    def test_partial_match_in_longer_text(self) -> None:
        """Detects Claude-specific usage-limit pattern embedded in longer text (A5-01).

        Plain "usage limit" without "Claude" must NOT match any more — it would
        false-trigger on GitHub's own "API usage limit" messages.
        """
        # Claude-prefixed form should still be detected
        text = "Claude API call failed: claude usage limit exceeded"
        assert detect_claude_usage_limit(text) is True

    def test_github_api_usage_limit_not_detected(self) -> None:
        """GitHub's own 'API usage limit' message must not be detected as Claude's (A5-01)."""
        text = "API call failed: usage limit exceeded, please try again later"
        assert detect_claude_usage_limit(text) is False

    def test_out_of_extra_usage_detected(self) -> None:
        """Detects 'out of extra usage' phrase used by Claude CLI."""
        result = detect_claude_usage_limit("You're out of extra usage for this period")
        assert result is True

    def test_upgrade_url_detected(self) -> None:
        """Detects claude.com/upgrade URL in error output."""
        result = detect_claude_usage_limit("Visit claude.com/upgrade to increase limits")
        assert result is True


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


class TestDetectSessionLimit:
    """Tests for detect_session_limit (#1321).

    The Claude CLI emits ``"You've hit your session limit · resets 4:20am"`` on
    a 429 — crucially WITHOUT the parenthesized timezone that
    detect_claude_usage_cap requires. The old two-detector logic missed it, so
    the orchestrator hard-failed instead of waiting for the reset.
    """

    def test_returns_none_for_unrelated_text(self) -> None:
        assert detect_session_limit("Normal output, nothing wrong") is None

    def test_parses_time_without_timezone(self) -> None:
        epoch = detect_session_limit("You've hit your session limit \xb7 resets 4:20am")
        assert epoch is not None
        assert epoch > int(time.time())

    def test_parses_time_with_timezone(self) -> None:
        text = "You've hit your session limit \xb7 resets 9pm (America/Los_Angeles)"
        epoch = detect_session_limit(text)
        assert epoch is not None
        assert epoch > int(time.time())

    def test_bare_message_returns_zero_sentinel(self) -> None:
        """A session-limit message with no parseable reset time yields 0.

        ``0`` means "rate-limited, reset unknown" — callers must back off
        rather than treat it as "no limit" (which ``None`` signals).
        """
        assert detect_session_limit("You've hit your session limit") == 0

    def test_finds_message_inside_json_payload(self) -> None:
        json_blob = (
            '{"is_error": true, "api_error_status": 429, '
            '"result": "You\'ve hit your session limit \xb7 resets 4:20am"}'
        )
        epoch = detect_session_limit(json_blob)
        assert epoch is not None
        assert epoch > int(time.time())


class TestResolveQuotaResetEpoch:
    """Tests for the single common resolver resolve_quota_reset_epoch (#1321).

    Every agent-invocation path routes through this one function, so it must
    recognize all three quota phrasings (GitHub limit, Claude usage cap, Claude
    session limit) and preserve the ``0`` unknown-reset sentinel.
    """

    def test_resolves_session_limit_without_timezone(self) -> None:
        epoch = resolve_quota_reset_epoch("You've hit your session limit \xb7 resets 4:20am")
        assert epoch is not None
        assert epoch > int(time.time())

    def test_resolves_github_rest_limit(self) -> None:
        epoch = resolve_quota_reset_epoch("Limit reached ... resets 2:30pm (America/Los_Angeles)")
        assert epoch is not None
        assert epoch > 0

    def test_resolves_claude_usage_cap(self) -> None:
        epoch = resolve_quota_reset_epoch(
            "Claude usage limit reached \xb7 resets 9pm (America/Los_Angeles)"
        )
        assert epoch is not None
        assert epoch > 0

    def test_returns_none_when_no_quota_message(self) -> None:
        assert resolve_quota_reset_epoch("nothing", "to see", "here") is None

    def test_skips_empty_streams_and_scans_all(self) -> None:
        epoch = resolve_quota_reset_epoch(
            "", "clean output", "You've hit your session limit \xb7 resets 4:20am"
        )
        assert epoch is not None
        assert epoch > int(time.time())

    def test_preserves_zero_unknown_reset_sentinel(self) -> None:
        # A bare session-limit message resolves to 0, which must be returned
        # (not skipped as falsy) so callers back off.
        assert resolve_quota_reset_epoch("You've hit your session limit") == 0


class TestGraphQLRateLimit:
    """Tests for the GraphQL rate-limit code path in detect_rate_limit()."""

    def setup_method(self) -> None:
        # Each test starts with a clean probe cache so mocks behave deterministically.
        _rate_limit_probe_cache.clear()

    def test_regex_matches_already_exceeded(self) -> None:
        text = "GraphQL: API rate limit already exceeded for user ID 4211002"
        assert GRAPHQL_RATE_LIMIT_RE.search(text) is not None

    def test_regex_matches_plain_exceeded(self) -> None:
        text = "API rate limit exceeded for installation ID 99"
        assert GRAPHQL_RATE_LIMIT_RE.search(text) is not None

    def test_regex_no_match_on_unrelated(self) -> None:
        assert GRAPHQL_RATE_LIMIT_RE.search("everything is fine") is None

    def test_detect_rate_limit_uses_probe_when_graphql_message_seen(self) -> None:
        """When the GraphQL phrase appears, fall back to gh_rate_limit_reset_epoch."""
        text = "GraphQL: API rate limit already exceeded for user ID 4211002"
        with patch(
            "hephaestus.github.rate_limit.gh_rate_limit_reset_epoch",
            return_value=1_700_000_000,
        ):
            assert detect_rate_limit(text) == 1_700_000_000

    def test_detect_rate_limit_returns_zero_sentinel_when_probe_fails(self) -> None:
        text = "GraphQL: API rate limit exceeded for user ID 1"
        with patch("hephaestus.github.rate_limit.gh_rate_limit_reset_epoch", return_value=None):
            assert detect_rate_limit(text) == 0

    def test_detect_rate_limit_prefers_rest_message_over_graphql(self) -> None:
        """REST message has a real reset time embedded; prefer it."""
        text = (
            "Limit reached for resource core, resets 2:30pm (UTC). "
            "GraphQL: API rate limit already exceeded."
        )
        # Should not call the probe — the REST regex matches first.
        with patch("hephaestus.github.rate_limit.gh_rate_limit_reset_epoch") as mock_probe:
            result = detect_rate_limit(text)
            mock_probe.assert_not_called()
        assert isinstance(result, int)
        assert result > 0


class TestGhRateLimitResetEpoch:
    """Tests for gh_rate_limit_reset_epoch() probe and cache."""

    def setup_method(self) -> None:
        _rate_limit_probe_cache.clear()

    def test_returns_reset_from_gh_api(self) -> None:
        payload = '{"resources": {"graphql": {"reset": 1700000000, "remaining": 0}}}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = payload
            mock_run.return_value.returncode = 0
            assert gh_rate_limit_reset_epoch() == 1700000000

    def test_caches_within_ttl(self) -> None:
        """Second call within TTL must not re-invoke gh."""
        payload = '{"resources": {"graphql": {"reset": 1700000000}}}'
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = payload
            mock_run.return_value.returncode = 0
            gh_rate_limit_reset_epoch()
            gh_rate_limit_reset_epoch()
            assert mock_run.call_count == 1

    def test_returns_none_on_subprocess_failure(self) -> None:
        import subprocess as sp

        with patch("subprocess.run", side_effect=sp.CalledProcessError(1, "gh")):
            assert gh_rate_limit_reset_epoch() is None

    def test_returns_none_on_invalid_json(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = "not json"
            mock_run.return_value.returncode = 0
            assert gh_rate_limit_reset_epoch() is None

    def test_returns_none_when_resource_missing(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.stdout = '{"resources": {}}'
            mock_run.return_value.returncode = 0
            assert gh_rate_limit_reset_epoch() is None


class TestGlobalThrottle:
    """Tests for gh_global_throttle_acquire (cross-process token bucket)."""

    def test_no_op_when_rate_zero(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HEPHAESTUS_GH_GLOBAL_RATE", "0")
        monkeypatch.setenv("HEPHAESTUS_RATE_DIR", str(tmp_path))
        # Should return effectively immediately and never touch the state file.
        before = time.monotonic()
        gh_global_throttle_acquire()
        elapsed = time.monotonic() - before
        assert elapsed < 0.05
        assert not (tmp_path / "hephaestus_gh_rate.json").exists()

    def test_first_call_succeeds_immediately_with_full_burst(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("HEPHAESTUS_GH_GLOBAL_RATE", "1000")
        monkeypatch.setenv("HEPHAESTUS_GH_GLOBAL_BURST", "10")
        monkeypatch.setenv("HEPHAESTUS_RATE_DIR", str(tmp_path))
        before = time.monotonic()
        gh_global_throttle_acquire()
        elapsed = time.monotonic() - before
        assert elapsed < 0.1
        assert (tmp_path / "hephaestus_gh_rate.json").exists()

    def test_rapid_calls_eventually_throttle(self, monkeypatch, tmp_path) -> None:
        """With burst=2 and rate=10/sec, the third call must request a sleep >= 0.1s.

        The throttle contract: when the token bucket is exhausted, ``time.sleep``
        is called with ``(1 - tokens) / rate`` seconds. We verify the *contract*
        deterministically by mocking ``time.sleep`` and ``time.monotonic`` in the
        production module, rather than measuring real wall-clock elapsed time
        (which is flaky on fast CI runners).

        ``time.monotonic`` is driven by a scripted sequence:

        * Calls 1 and 2 each consume one iteration of the while-loop; monotonic
          returns the same ``t0`` for both so zero time appears to pass and no
          refill occurs between them.
        * Call 3's first loop iteration also sees ``t0`` → bucket is empty →
          the throttle records ``wait = 0.1s`` and calls ``time.sleep(0.1)``.
        * Call 3's *retry* iteration sees ``t0 + 0.2`` → the bucket has refilled
          by ``0.2 * 10 = 2 tokens``, which is enough to consume one token and
          return. This lets the loop terminate so the test completes.

        The sleep-call list is the observable contract: we assert that at least
        one call requested >= 0.09s (not a loose zero-threshold; the expected
        value is exactly ``(1.0 - 0) / 10 = 0.1s``).
        """
        import unittest.mock

        monkeypatch.setenv("HEPHAESTUS_GH_GLOBAL_RATE", "10")
        monkeypatch.setenv("HEPHAESTUS_GH_GLOBAL_BURST", "2")
        monkeypatch.setenv("HEPHAESTUS_RATE_DIR", str(tmp_path))

        sleep_calls: list[float] = []

        # Scripted monotonic sequence:
        #   t0        – iteration for call 1 (succeeds, 2→1 tokens)
        #   t0        – iteration for call 2 (succeeds, 1→0 tokens)
        #   t0        – first iteration for call 3 (empty bucket → sleep queued)
        #   t0 + 0.2  – retry iteration for call 3 (0.2s of refill → 2 tokens → succeed)
        #   (extra values guard against off-by-one in the iteration count)
        t0 = 1_000.0
        mono_values = iter([t0, t0, t0, t0 + 0.2, t0 + 0.2, t0 + 0.2])

        with (
            unittest.mock.patch(
                "hephaestus.github.rate_limit.time.sleep",
                side_effect=lambda s: sleep_calls.append(s),
            ),
            unittest.mock.patch(
                "hephaestus.github.rate_limit.time.monotonic",
                side_effect=mono_values,
            ),
        ):
            gh_global_throttle_acquire()  # consumes token 1 (burst=2 → 1 left)
            gh_global_throttle_acquire()  # consumes token 2 (1 → 0 left)
            gh_global_throttle_acquire()  # bucket empty → must sleep and retry

        # The throttle must have called sleep at least once with a wait that
        # reflects the cost of refilling 1 token at 10/sec (= 0.1s).
        assert sleep_calls, "throttle must call time.sleep when bucket is exhausted"
        assert sleep_calls[0] >= 0.09, (
            f"throttle sleep was {sleep_calls[0]:.4f}s; expected >= 0.09s "
            f"(1 token at 10/sec costs 0.1s)"
        )


class TestCountdownThrottle:
    """Tests for _countdown_loop TTY vs non-TTY emission throttling (#1330)."""

    @patch("hephaestus.github.rate_limit.logger")
    @patch("hephaestus.github.rate_limit.print")
    @patch("hephaestus.github.rate_limit.sys.stdout")
    @patch("hephaestus.github.rate_limit.time.monotonic")
    @patch("hephaestus.github.rate_limit.time.sleep")
    @patch("hephaestus.github.rate_limit.time.time")
    def test_non_tty_throttles_to_ten_minute_emissions(
        self,
        mock_time: object,
        mock_sleep: object,
        mock_monotonic: object,
        mock_stdout: object,
        mock_print: object,
        mock_logger: object,
    ) -> None:
        """Non-TTY: a ~30-minute wait logs ~3 lines (one per 10 min), not ~1800."""
        import unittest.mock

        assert isinstance(mock_time, unittest.mock.MagicMock)
        assert isinstance(mock_monotonic, unittest.mock.MagicMock)
        assert isinstance(mock_stdout, unittest.mock.MagicMock)
        assert isinstance(mock_logger, unittest.mock.MagicMock)
        assert isinstance(mock_print, unittest.mock.MagicMock)

        mock_stdout.isatty.return_value = False

        # Wall-clock and monotonic both advance 1 second per iteration so the
        # 600s throttle window maps cleanly onto a 1800-iteration countdown.
        target = 1800
        mock_time.side_effect = list(range(0, target + 2))
        mock_monotonic.side_effect = [float(i) for i in range(0, target + 2)]

        _countdown_loop(target, lambda: False)

        # Non-TTY must never use the \r spinner.
        mock_print.assert_not_called()
        # Emissions at t=0, 600, 1200 → exactly 3 over a 30-minute wait.
        info_calls = list(mock_logger.info.call_args_list)
        assert len(info_calls) == 3, f"expected 3 throttled emissions, got {len(info_calls)}"

    @patch("hephaestus.github.rate_limit.logger")
    @patch("hephaestus.github.rate_limit.print")
    @patch("hephaestus.github.rate_limit.sys.stdout")
    @patch("hephaestus.github.rate_limit.time.sleep")
    @patch("hephaestus.github.rate_limit.time.time")
    def test_tty_uses_one_hz_spinner(
        self,
        mock_time: object,
        mock_sleep: object,
        mock_stdout: object,
        mock_print: object,
        mock_logger: object,
    ) -> None:
        r"""TTY mode prints the \r spinner per tick and does not log progress."""
        import unittest.mock

        assert isinstance(mock_time, unittest.mock.MagicMock)
        assert isinstance(mock_stdout, unittest.mock.MagicMock)
        assert isinstance(mock_print, unittest.mock.MagicMock)
        assert isinstance(mock_logger, unittest.mock.MagicMock)

        mock_stdout.isatty.return_value = True

        target = 1000
        # now, now, now, then past-epoch → two spinner ticks then return.
        mock_time.side_effect = [997, 998, 999, 1001]

        _countdown_loop(target, lambda: False)

        # Spinner printed each tick (plus the trailing newline on completion).
        assert mock_print.call_count >= 2
        spinner_calls = [
            c
            for c in mock_print.call_args_list
            if c.args and "Rate limit resets in" in str(c.args[0])
        ]
        assert spinner_calls, "expected at least one \\r spinner print on a TTY"
        # TTY mode must not emit throttled INFO progress lines.
        mock_logger.info.assert_not_called()
