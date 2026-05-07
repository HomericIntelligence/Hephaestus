"""Tests for the review-verdict parser used by the strict review loops."""

from __future__ import annotations

from hephaestus.automation.claude_invoke import ReviewVerdict, parse_review_verdict


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
