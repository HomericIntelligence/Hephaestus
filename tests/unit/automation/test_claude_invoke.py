"""Tests for the review-verdict parser used by the strict review loops."""

from __future__ import annotations

import subprocess

import pytest
from hypothesis import given, strategies as st

from hephaestus.automation.claude_invoke import (
    INFRA_ERROR_REVIEW_TEXT,
    ReviewVerdict,
    describe_claude_failure,
    detect_model_usage_cap,
    detect_server_overload,
    format_called_process_error,
    parse_review_verdict,
)

# The exact model-cap phrasing observed in output2.log (2026-07-03). Unlike a
# session-limit 429 it carries NO reset time — the remediation is a model
# switch, not a wait (#1793).
MODEL_CAP_MESSAGE = (
    "You've reached your Fable 5 limit. "
    "Run /usage-credits to continue or switch models with /model."
)


class TestDetectModelUsageCap:
    """Tests for the model-specific usage-cap classifier (#1793)."""

    def test_production_phrasing(self) -> None:
        """The exact envelope text from the 2026-07-03 loop run is detected."""
        assert detect_model_usage_cap(MODEL_CAP_MESSAGE) is True

    def test_other_model_name(self) -> None:
        """The regex generalizes to any model tier name, not just Fable."""
        assert detect_model_usage_cap("You've reached your Opus 4.8 limit.") is True

    def test_phrase_signals_alone(self) -> None:
        """Each remediation-hint phrase is an independent signal."""
        assert detect_model_usage_cap("Run /usage-credits to continue") is True
        assert detect_model_usage_cap("switch models with /model") is True

    def test_session_limit_not_matched(self) -> None:
        """Session-limit phrasings stay owned by detect_session_limit (wait path)."""
        assert (
            detect_model_usage_cap(
                "You've hit your session limit · resets 12:30pm (America/Los_Angeles)"
            )
            is False
        )
        assert detect_model_usage_cap("You've reached your session limit") is False

    def test_usage_cap_not_matched(self) -> None:
        """Generic usage-cap phrasings stay owned by detect_claude_usage_cap."""
        assert (
            detect_model_usage_cap(
                "You're out of extra usage · resets May 8, 5pm (America/Los_Angeles)"
            )
            is False
        )
        assert detect_model_usage_cap("You've reached your usage limit") is False

    def test_scans_multiple_streams(self) -> None:
        """Detection spans both stderr and stdout streams."""
        assert detect_model_usage_cap("clean stderr", MODEL_CAP_MESSAGE) is True

    def test_empty_and_no_streams(self) -> None:
        """Empty or absent streams are skipped without error."""
        assert detect_model_usage_cap("", "") is False
        assert detect_model_usage_cap() is False

    def test_model_cap_has_no_reset_epoch(self) -> None:
        """Documents WHY fallback (not wait): the message yields no reset epoch."""
        from hephaestus.github.rate_limit import resolve_quota_reset_epoch

        assert resolve_quota_reset_epoch(MODEL_CAP_MESSAGE) is None


class TestDetectServerOverload:
    """Tests for the transient server-overload classifier (#1374)."""

    def test_529_overloaded_api_error(self) -> None:
        """The exact phrasing from output.log L30/L414 is retryable."""
        assert detect_server_overload("Claude failed: API Error: 529 Overloaded") is True

    def test_overloaded_word_alone(self) -> None:
        """A bare ``Overloaded`` token is recognized."""
        assert detect_server_overload("", "the service is Overloaded right now") is True

    def test_overloaded_error_json(self) -> None:
        """The Anthropic ``overloaded_error`` JSON payload is recognized."""
        assert detect_server_overload('{"error":{"type":"overloaded_error"}}') is True

    def test_5xx_statuses_retryable(self) -> None:
        """Generic 5xx overload statuses are retryable."""
        assert detect_server_overload("API Error: 503 Service Unavailable") is True
        assert detect_server_overload("status code: 502 Bad Gateway") is True
        assert detect_server_overload("status 504") is True
        assert detect_server_overload("API Error: 500 Internal Server Error") is True

    def test_scans_multiple_streams(self) -> None:
        """Detection spans both stderr and stdout streams."""
        assert detect_server_overload("clean stderr", "API Error: 529 Overloaded") is True

    def test_quota_429_not_overload(self) -> None:
        """A 429 quota cap is NOT an overload (handled by scan_quota_reset)."""
        assert detect_server_overload("API Error: 429 rate limit exceeded") is False

    def test_fatal_4xx_not_overload(self) -> None:
        """Genuinely fatal client errors stay fatal — no over-broad retry."""
        assert detect_server_overload("API Error: 400 Bad Request") is False
        assert detect_server_overload("API Error: 401 Unauthorized") is False

    def test_unrelated_529_digits_not_matched(self) -> None:
        """A bare ``529`` without an error/status context is not matched."""
        assert detect_server_overload("processed 529 files successfully") is False

    def test_empty_and_none_streams(self) -> None:
        """Empty or falsy streams are skipped without error."""
        assert detect_server_overload("", "") is False
        assert detect_server_overload() is False


class TestParseReviewVerdict:
    """Tests for parsing Grade and Verdict lines from review output."""

    def test_unambiguous_go(self) -> None:
        """Parse a clean GO with letter grade."""
        v = parse_review_verdict("blah blah\nGrade: A\nVerdict: GO\n")
        assert v == ReviewVerdict(grade="A", verdict="GO", raw=v.raw)
        assert v.is_go is True

    def test_unambiguous_nogo(self) -> None:
        """Parse a clean NOGO with letter+plus grade."""
        v = parse_review_verdict("Grade: D+\nVerdict: NOGO")
        assert v.grade == "D+"
        assert v.verdict == "NOGO"
        assert v.is_go is False

    def test_no_go_with_dash(self) -> None:
        """Accept `NO-GO` as a NOGO verdict."""
        v = parse_review_verdict("Grade: F\nVerdict: NO-GO")
        assert v.verdict == "NOGO"

    def test_no_go_with_space(self) -> None:
        """Accept `NO GO` as a NOGO verdict."""
        v = parse_review_verdict("Grade: F\nVerdict: NO GO")
        assert v.verdict == "NOGO"

    def test_missing_verdict_is_ambiguous(self) -> None:
        """Missing verdict => AMBIGUOUS, treated as not-GO by the loop."""
        v = parse_review_verdict("Grade: B")
        assert v.verdict == "AMBIGUOUS"
        assert v.is_go is False

    def test_missing_grade_only_verdict(self) -> None:
        """Verdict without grade is still actionable."""
        v = parse_review_verdict("Verdict: GO")
        assert v.grade is None
        assert v.verdict == "GO"
        assert v.is_go is True

    def test_with_bold_markers(self) -> None:
        """Markdown bold around the labels is tolerated."""
        v = parse_review_verdict("**Grade:** B+\n**Verdict:** GO")
        assert v.grade == "B+"
        assert v.verdict == "GO"

    def test_case_insensitive(self) -> None:
        """Lowercase labels still match."""
        v = parse_review_verdict("grade: c-\nverdict: nogo")
        assert v.grade == "C-"
        assert v.verdict == "NOGO"


class TestInfraErrorVerdict:
    """Reviewer-infrastructure failures parse to a distinct ERROR verdict.

    A 400/timeout/crash from the reviewer subprocess must NOT be laundered into
    a real ``NOGO`` — that would burn review iterations and trigger a spurious
    ``state:skip`` on a PR that was never actually reviewed (#911 / PR #1069).
    """

    def test_sentinel_text_parses_to_error_verdict(self) -> None:
        """The infra-error sentinel text resolves to verdict=ERROR."""
        v = parse_review_verdict(INFRA_ERROR_REVIEW_TEXT)
        assert v.verdict == "ERROR"
        assert v.is_error is True
        assert v.is_go is False

    def test_error_verdict_round_trips_through_text(self) -> None:
        """An ERROR verdict survives the text → log → re-parse round-trip.

        The loop persists ``review_text`` and re-parses it, so the sentinel
        must be recognizable as ERROR on a second parse, not collapse to NOGO.
        """
        first = parse_review_verdict(
            f"Reviewer crashed at iteration 2\n\n{INFRA_ERROR_REVIEW_TEXT}"
        )
        assert first.verdict == "ERROR"
        assert parse_review_verdict(first.raw).verdict == "ERROR"

    def test_real_nogo_is_not_error(self) -> None:
        """A genuine reviewer NOGO is distinct from an infra ERROR."""
        v = parse_review_verdict("Grade: F\nVerdict: NOGO")
        assert v.verdict == "NOGO"
        assert v.is_error is False

    def test_go_is_not_error(self) -> None:
        """A GO verdict is not an error."""
        v = parse_review_verdict("Grade: A\nVerdict: GO")
        assert v.is_error is False


class TestRaiseForErrorEnvelope:
    """Tests for the central is_error JSON envelope guard (#1528 follow-up)."""

    def test_429_envelope_raises_usage_cap_with_reset(self) -> None:
        """A 429 envelope raises ClaudeUsageCapError carrying the reset epoch."""
        import json

        from hephaestus.automation.claude_invoke import raise_for_error_envelope
        from hephaestus.github.client import ClaudeUsageCapError

        stdout = json.dumps(
            {
                "is_error": True,
                "api_error_status": 429,
                "result": "You've hit your session limit · resets 5pm (America/Los_Angeles)",
            }
        )
        with pytest.raises(ClaudeUsageCapError) as exc:
            raise_for_error_envelope(stdout)
        assert exc.value.reset_epoch is not None and exc.value.reset_epoch > 0

    def test_non_quota_error_envelope_raises_runtime_error(self) -> None:
        """A non-quota is_error envelope raises a plain RuntimeError."""
        import json

        from hephaestus.automation.claude_invoke import raise_for_error_envelope
        from hephaestus.github.client import ClaudeUsageCapError

        stdout = json.dumps({"is_error": True, "result": "tool execution failed"})
        with pytest.raises(RuntimeError) as exc:
            raise_for_error_envelope(stdout)
        assert not isinstance(exc.value, ClaudeUsageCapError)

    def test_success_envelope_does_not_raise(self) -> None:
        """A normal (is_error absent/false) result is left untouched."""
        import json

        from hephaestus.automation.claude_invoke import raise_for_error_envelope

        raise_for_error_envelope(json.dumps({"result": "Grade: A\nVerdict: GO"}))
        raise_for_error_envelope(json.dumps({"is_error": False, "result": "ok"}))

    def test_non_json_stdout_does_not_raise(self) -> None:
        """Plain-text or empty stdout is ignored (not every caller uses json)."""
        from hephaestus.automation.claude_invoke import raise_for_error_envelope

        raise_for_error_envelope("just some prose")
        raise_for_error_envelope("")


class TestFormatCalledProcessError:
    """Edge cases for the bounded stdout/stderr formatter (#1799)."""

    @staticmethod
    def _error(
        output: str | bytes | None = None, stderr: str | bytes | None = None
    ) -> subprocess.CalledProcessError:
        return subprocess.CalledProcessError(
            returncode=1, cmd=["claude", "-p"], output=output, stderr=stderr
        )

    def test_bytes_streams_are_decoded(self) -> None:
        """Bytes stdout/stderr (non-text subprocess mode) decode into the message."""
        err = self._error(stderr=b"boom stderr", output=b"boom stdout")
        detail = format_called_process_error(err)
        assert "stderr='boom stderr'" in detail
        assert "stdout='boom stdout'" in detail

    def test_invalid_utf8_bytes_are_replaced_not_raised(self) -> None:
        """Undecodable bytes degrade via errors='replace' instead of raising."""
        err = self._error(stderr=b"\xff\xfe bad")
        detail = format_called_process_error(err)
        assert "stderr=" in detail
        assert "�" in detail

    def test_truncates_beyond_500_chars(self) -> None:
        """Each stream is bounded at 500 chars plus an explicit truncation marker."""
        err = self._error(stderr="x" * 501)
        detail = format_called_process_error(err)
        assert "x" * 500 + "... [truncated]" in detail
        assert "x" * 501 not in detail

    def test_exactly_500_chars_is_not_truncated(self) -> None:
        err = self._error(stderr="y" * 500)
        detail = format_called_process_error(err)
        assert "y" * 500 in detail
        assert "[truncated]" not in detail

    def test_stdout_only_when_stderr_absent(self) -> None:
        """With no stderr, stdout still surfaces (some CLIs report errors there)."""
        err = self._error(output="only stdout detail")
        detail = format_called_process_error(err)
        assert "stdout='only stdout detail'" in detail
        assert "stderr=" not in detail

    def test_no_streams_falls_back_to_str(self) -> None:
        err = self._error()
        assert format_called_process_error(err) == str(err)

    def test_custom_max_chars(self) -> None:
        err = self._error(stderr="abcdef")
        detail = format_called_process_error(err, max_chars=3)
        assert "abc... [truncated]" in detail


class TestDescribeClaudeFailure:
    """Dispatch helper shared by the advise/implementer/post-merge call sites."""

    def test_called_process_error_gets_stream_detail(self) -> None:
        err = subprocess.CalledProcessError(
            returncode=1, cmd=["claude"], stderr="No conversation found"
        )
        detail = describe_claude_failure(err)
        assert detail == format_called_process_error(err)
        assert "No conversation found" in detail

    def test_other_exceptions_fall_back_to_str(self) -> None:
        assert describe_claude_failure(TimeoutError("too slow")) == "too slow"


# Verdict fragments Hypothesis interleaves with arbitrary text to exercise the
# structured branches of the parser, not just unicode noise (issue #1470).
_VERDICT_TOKENS = ["GO", "NOGO", "NO-GO", "NO GO", "ERROR"]


class TestParseReviewVerdictProperties:
    """Property-based fuzz coverage for parse_review_verdict (#1470)."""

    @given(st.text())
    def test_never_raises_and_preserves_raw(self, text: str) -> None:
        result = parse_review_verdict(text)
        assert isinstance(result, ReviewVerdict)
        assert result.raw == text
        assert result.verdict in {"GO", "NOGO", "ERROR", "AMBIGUOUS"}

    @given(st.text())
    def test_no_verdict_marker_is_ambiguous(self, text: str) -> None:
        # Text with no "verdict" token must resolve to AMBIGUOUS (fail-safe).
        if "verdict" not in text.lower():
            assert parse_review_verdict(text).verdict == "AMBIGUOUS"

    @given(st.sampled_from(_VERDICT_TOKENS), st.text(max_size=200))
    def test_anchored_verdict_line_classifies(self, token: str, noise: str) -> None:
        body = f"{noise}\nVerdict: {token}\n"
        normalized = token.replace("-", "").replace(" ", "")
        expected = {"GO": "GO", "ERROR": "ERROR"}.get(normalized, "NOGO")
        assert parse_review_verdict(body).verdict == expected
