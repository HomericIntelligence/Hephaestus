"""Tests for the review-verdict parser used by the strict review loops."""

from __future__ import annotations

from hephaestus.automation.claude_invoke import (
    INFRA_ERROR_REVIEW_TEXT,
    ReviewVerdict,
    parse_review_verdict,
)


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
