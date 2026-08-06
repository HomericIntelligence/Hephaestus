"""Tests for the Bandit LOW-severity baseline drift checker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.ci.bandit_baseline_check import (
    count_by_test_id,
    diff_against_baseline,
    main,
)


def test_count_by_test_id_tallies_duplicate_low_results() -> None:
    """Duplicate LOW-severity findings are accumulated by test ID."""
    report = {
        "results": [
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B311", "issue_severity": "LOW"},
        ]
    }
    assert count_by_test_id(report) == {"B607": 2, "B311": 1}


def test_count_by_test_id_excludes_medium_and_high_severity() -> None:
    """Medium and high findings do not enter the LOW-only baseline."""
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
    """A test ID absent from the baseline is a regression."""
    problems = diff_against_baseline({"B311": 1, "B999": 1}, {"B311": 1})
    assert problems == ["REGRESSION: B999 is new (0 -> 1)"]


def test_diff_flags_increased_count() -> None:
    """A count higher than the baseline is a regression."""
    problems = diff_against_baseline({"B607": 30}, {"B607": 23})
    assert problems == ["REGRESSION: B607 count increased (23 -> 30)"]


def test_diff_flags_decreased_count_as_stale() -> None:
    """A lower observed count identifies a stale baseline entry."""
    problems = diff_against_baseline({"B607": 10}, {"B607": 23})
    assert problems == ["STALE BASELINE: B607 count decreased (23 -> 10)"]


def test_diff_flags_missing_current_test_id_as_stale() -> None:
    """A baseline ID absent from the report identifies a stale entry."""
    problems = diff_against_baseline({}, {"B607": 23})
    assert problems == ["STALE BASELINE: B607 is no longer observed (23 -> 0)"]


def test_diff_clean_when_matching() -> None:
    """Matching counts produce no drift."""
    assert diff_against_baseline({"B311": 1, "B607": 23}, {"B311": 1, "B607": 23}) == []


def test_main_prints_regression_and_stale_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI output distinguishes increased findings from stale entries."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "results": [
                    {"test_id": "B999", "issue_severity": "LOW"},
                    {"test_id": "B607", "issue_severity": "LOW"},
                ]
            }
        ),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(
            {
                "generated_by": "issue #1481",
                "severity": "LOW",
                "counts": {"B607": 2, "B311": 1},
            }
        ),
        encoding="utf-8",
    )

    assert main([str(report_path), str(baseline_path)]) == 1
    output = capsys.readouterr().out
    assert "REGRESSION: B999" in output
    assert "STALE BASELINE: B607" in output
    assert "STALE BASELINE: B311" in output


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"results": {}},
        {"results": [None]},
        {"results": [{"issue_severity": "LOW"}]},
        {"results": [{"test_id": "B607"}]},
        {"results": [{"test_id": "", "issue_severity": "LOW"}]},
        {"results": [{"test_id": "B607", "issue_severity": ""}]},
    ],
)
def test_count_by_test_id_rejects_malformed_report(report: dict[str, object]) -> None:
    """Report structure and required finding fields are validated."""
    with pytest.raises(ValueError):
        count_by_test_id(report)


@pytest.mark.parametrize(
    "baseline",
    [
        {},
        {"generated_by": "issue #1", "severity": "HIGH", "counts": {}},
        {"generated_by": "", "severity": "LOW", "counts": {}},
        {"generated_by": "issue #1", "severity": "LOW"},
        {"generated_by": "issue #1", "severity": "LOW", "counts": []},
        {"generated_by": "issue #1", "severity": "LOW", "counts": {"B607": 0}},
        {"generated_by": "issue #1", "severity": "LOW", "counts": {"B607": -1}},
        {"generated_by": "issue #1", "severity": "LOW", "counts": {"B607": True}},
        {"generated_by": "issue #1", "severity": "LOW", "counts": {"": 1}},
    ],
)
def test_baseline_counts_rejects_malformed_baseline(
    tmp_path: Path,
    baseline: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Baseline metadata and positive integer counts are validated."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"results": []}', encoding="utf-8")
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")

    assert main([str(report_path), str(baseline_path)]) == 2
    assert "invalid Bandit baseline input" in capsys.readouterr().err


def test_main_rejects_duplicate_json_keys(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Duplicate JSON object keys are treated as malformed input."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"results": [], "results": []}', encoding="utf-8")
    baseline_path.write_text(
        '{"generated_by": "issue #1", "severity": "LOW", "counts": {}}',
        encoding="utf-8",
    )

    assert main([str(report_path), str(baseline_path)]) == 2
    assert "duplicate JSON key" in capsys.readouterr().err


def test_main_returns_two_for_missing_baseline(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unreadable baseline is a malformed-input failure."""
    report_path = tmp_path / "report.json"
    report_path.write_text('{"results": []}', encoding="utf-8")

    assert main([str(report_path), str(tmp_path / "missing.json")]) == 2
    assert "invalid Bandit baseline input" in capsys.readouterr().err


def test_update_baseline_requires_review_reference(tmp_path: Path) -> None:
    """Baseline updates cannot run without a review reference."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"results": []}', encoding="utf-8")

    with pytest.raises(SystemExit):
        main([str(report_path), str(baseline_path), "--update-baseline"])
    assert not baseline_path.exists()


def test_review_reference_requires_update_mode(tmp_path: Path) -> None:
    """A review reference cannot alter normal read-only comparison mode."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"results": []}', encoding="utf-8")
    baseline_path.write_text(
        '{"generated_by": "issue #1", "severity": "LOW", "counts": {}}',
        encoding="utf-8",
    )

    with pytest.raises(SystemExit):
        main(
            [
                str(report_path),
                str(baseline_path),
                "--review-reference",
                "issue #2384",
            ]
        )


def test_update_baseline_writes_canonical_counts(tmp_path: Path) -> None:
    """A reviewed update writes sorted, accumulated LOW counts."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "results": [
                    {"test_id": "B607", "issue_severity": "LOW"},
                    {"test_id": "B311", "issue_severity": "LOW"},
                    {"test_id": "B607", "issue_severity": "LOW"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                str(report_path),
                str(baseline_path),
                "--update-baseline",
                "--review-reference",
                "issue #2384",
            ]
        )
        == 0
    )
    assert json.loads(baseline_path.read_text(encoding="utf-8")) == {
        "generated_by": "issue #2384",
        "severity": "LOW",
        "counts": {"B311": 1, "B607": 2},
    }
    assert main([str(report_path), str(baseline_path)]) == 0
