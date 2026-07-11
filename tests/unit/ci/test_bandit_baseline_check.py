"""Tests for the bandit LOW-severity baseline drift checker."""

from __future__ import annotations

from hephaestus.ci.bandit_baseline_check import count_by_test_id, diff_against_baseline


def test_count_by_test_id_tallies_low_severity_results() -> None:
    """Tallies LOW-severity findings by test_id."""
    report = {
        "results": [
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B311", "issue_severity": "LOW"},
        ]
    }
    assert count_by_test_id(report) == {"B607": 2, "B311": 1}


def test_count_by_test_id_excludes_medium_and_high_severity() -> None:
    """--severity-level low is a minimum threshold; MEDIUM/HIGH must not leak into the ledger."""
    report = {
        "results": [
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B602", "issue_severity": "HIGH"},
            {"test_id": "B608", "issue_severity": "MEDIUM"},
        ]
    }
    assert count_by_test_id(report) == {"B607": 1}


def test_count_by_test_id_empty_results() -> None:
    """Empty results tally to an empty dict."""
    assert count_by_test_id({"results": []}) == {}


def test_diff_flags_new_test_id() -> None:
    """A test_id absent from the baseline is flagged as new."""
    problems = diff_against_baseline({"B311": 1, "B999": 1}, {"B311": 1})
    assert len(problems) == 1
    assert "B999" in problems[0]


def test_diff_flags_increased_count() -> None:
    """A count higher than the baseline is flagged as drift."""
    problems = diff_against_baseline({"B607": 30}, {"B607": 23})
    assert len(problems) == 1
    assert "23 -> 30" in problems[0]


def test_diff_allows_decreased_count() -> None:
    """A count lower than the baseline is not flagged."""
    assert diff_against_baseline({"B607": 10}, {"B607": 23}) == []


def test_diff_clean_when_matching() -> None:
    """Matching counts produce no drift."""
    assert diff_against_baseline({"B311": 1, "B607": 23}, {"B311": 1, "B607": 23}) == []
