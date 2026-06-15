"""Tests for the review-verdict parser used by the strict review loops."""

from __future__ import annotations

from hephaestus.automation.claude_invoke import (
    INFRA_ERROR_REVIEW_TEXT,
    ReviewVerdict,
    detect_server_overload,
    parse_review_verdict,
)


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
