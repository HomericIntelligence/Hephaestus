"""Tests for the Bandit LOW-severity baseline drift checker."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.ci.bandit_baseline_check import (
    count_by_file_test_id,
    count_by_test_id,
    diff_against_baseline,
    diff_files_against_baseline,
    main,
)
from hephaestus.cli.localization import using_localizer


def test_count_by_test_id_tallies_duplicate_low_results() -> None:
    """Duplicate LOW-severity findings are accumulated by test ID."""
    report = {
        "errors": [],
        "results": [
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B311", "issue_severity": "LOW"},
        ],
    }
    assert count_by_test_id(report) == {"B607": 2, "B311": 1}


def test_count_by_test_id_excludes_medium_and_high_severity() -> None:
    """Medium and high findings do not enter the LOW-only baseline."""
    report = {
        "errors": [],
        "results": [
            {"test_id": "B607", "issue_severity": "LOW"},
            {"test_id": "B602", "issue_severity": "HIGH"},
            {"test_id": "B608", "issue_severity": "MEDIUM"},
        ],
    }
    assert count_by_test_id(report) == {"B607": 1}


def test_count_by_test_id_empty_results() -> None:
    """Empty results tally to an empty dict."""
    assert count_by_test_id({"errors": [], "results": []}) == {}


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    [
        pytest.param(
            {"B311": 1, "B607": 23},
            {"B311": 1, "B607": 23},
            [],
            id="unchanged",
        ),
        pytest.param(
            {"B607": 30},
            {"B607": 23},
            ["REGRESSION: B607 count increased (23 -> 30)"],
            id="increased-count",
        ),
        pytest.param(
            {"B607": 10},
            {"B607": 23},
            ["STALE BASELINE: B607 count decreased (23 -> 10)"],
            id="decreased-count",
        ),
        pytest.param(
            {"B311": 1, "B999": 1},
            {"B311": 1},
            ["REGRESSION: B999 is new (0 -> 1)"],
            id="new-finding-id",
        ),
        pytest.param(
            {},
            {"B607": 23},
            ["STALE BASELINE: B607 is no longer observed (23 -> 0)"],
            id="removed-finding-id",
        ),
    ],
)
def test_diff_classifies_finding_changes(
    current: dict[str, int],
    baseline: dict[str, int],
    expected: list[str],
) -> None:
    """The comparator classifies each supported finding-count change."""
    assert diff_against_baseline(current, baseline) == expected


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
                "errors": [],
                "results": [
                    {"test_id": "B999", "issue_severity": "LOW"},
                    {"test_id": "B607", "issue_severity": "LOW"},
                ],
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


def test_main_localizes_classified_problem_templates(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Translate classification prose and keep finding identifiers and counts."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "errors": [],
                "results": [{"test_id": "B999", "issue_severity": "LOW"}],
            }
        ),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps({"generated_by": "issue #1", "severity": "LOW", "counts": {}}),
        encoding="utf-8",
    )
    source = "REGRESSION: %(test_id)s is new (%(baseline)d -> %(current)d)"

    with using_localizer(
        {source: "RÉGRESSION : %(test_id)s est nouveau (%(baseline)d -> %(current)d)"}
    ):
        assert main([str(report_path), str(baseline_path)]) == 1

    output = capsys.readouterr().out
    assert "RÉGRESSION : B999 est nouveau (0 -> 1)" in output


@pytest.mark.parametrize(
    "report",
    [
        {},
        {"errors": None, "results": []},
        {"errors": {}, "results": []},
        {"errors": [], "results": {}},
        {"errors": [], "results": [None]},
        {"errors": [], "results": [{"issue_severity": "LOW"}]},
        {"errors": [], "results": [{"test_id": "B607"}]},
        {"errors": [], "results": [{"test_id": "", "issue_severity": "LOW"}]},
        {"errors": [], "results": [{"test_id": "B607", "issue_severity": ""}]},
    ],
)
def test_count_by_test_id_rejects_malformed_report(report: dict[str, object]) -> None:
    """Report structure and required finding fields are validated."""
    with pytest.raises(ValueError):
        count_by_test_id(report)


@pytest.mark.parametrize(
    "report",
    [
        {"results": []},
        {"errors": None, "results": []},
        {"errors": {}, "results": []},
    ],
)
def test_main_returns_two_for_malformed_errors(
    tmp_path: Path,
    report: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing or non-list Bandit errors are malformed CLI input."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")
    baseline_path.write_text(
        '{"generated_by": "issue #1", "severity": "LOW", "counts": {}}',
        encoding="utf-8",
    )

    assert main([str(report_path), str(baseline_path)]) == 2
    assert "Bandit report must define an 'errors' list" in capsys.readouterr().err


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
    report_path.write_text('{"errors": [], "results": []}', encoding="utf-8")
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
    report_path.write_text(
        '{"errors": [], "results": [], "results": []}',
        encoding="utf-8",
    )
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
    report_path.write_text('{"errors": [], "results": []}', encoding="utf-8")

    assert main([str(report_path), str(tmp_path / "missing.json")]) == 2
    assert "invalid Bandit baseline input" in capsys.readouterr().err


def test_update_baseline_requires_review_reference(tmp_path: Path) -> None:
    """Baseline updates cannot run without a review reference."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"errors": [], "results": []}', encoding="utf-8")

    with pytest.raises(SystemExit):
        main([str(report_path), str(baseline_path), "--update-baseline"])
    assert not baseline_path.exists()


def test_review_reference_requires_update_mode(tmp_path: Path) -> None:
    """A review reference cannot alter normal read-only comparison mode."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text('{"errors": [], "results": []}', encoding="utf-8")
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
                "errors": [],
                "results": [
                    {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
                    {"test_id": "B311", "issue_severity": "LOW", "filename": "hephaestus/b.py"},
                    {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
                ],
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
        "files": {"hephaestus/a.py::B607": 2, "hephaestus/b.py::B311": 1},
    }
    assert main([str(report_path), str(baseline_path)]) == 0


def test_update_baseline_rejects_report_errors_without_rewriting(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A partial Bandit report cannot replace the reviewed baseline."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "errors": [{"filename": "unscannable.py", "reason": "parse failure"}],
                "results": [],
            }
        ),
        encoding="utf-8",
    )
    original_baseline = '{"generated_by": "issue #1", "severity": "LOW", "counts": {"B607": 1}}\n'
    baseline_path.write_text(original_baseline, encoding="utf-8")

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
        == 2
    )
    assert baseline_path.read_text(encoding="utf-8") == original_baseline
    assert "Bandit report contains scan errors" in capsys.readouterr().err


def test_count_by_file_test_id_tallies_per_file() -> None:
    """Per-file LOW counts are keyed by ``filename::test_id``."""
    report = {
        "errors": [],
        "results": [
            {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
            {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
            {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/b.py"},
            {"test_id": "B311", "issue_severity": "LOW", "filename": "hephaestus/b.py"},
            {"test_id": "B607", "issue_severity": "HIGH", "filename": "hephaestus/a.py"},
        ],
    }
    assert count_by_file_test_id(report) == {
        "hephaestus/a.py::B607": 2,
        "hephaestus/b.py::B607": 1,
        "hephaestus/b.py::B311": 1,
    }


@pytest.mark.parametrize(
    "filename",
    [None, "", "   ", 42, {"a": "b"}],
    ids=["missing", "empty", "whitespace", "int", "dict"],
)
def test_count_by_file_test_id_rejects_invalid_filename(filename: object) -> None:
    """Per-file counting rejects missing or invalid filenames."""
    report = {
        "errors": [],
        "results": [
            {
                "test_id": "B607",
                "issue_severity": "LOW",
                **({} if filename is None else {"filename": filename}),
            }
        ],
    }
    with pytest.raises(ValueError, match="invalid filename"):
        count_by_file_test_id(report)


@pytest.mark.parametrize(
    ("current", "baseline", "expected"),
    [
        pytest.param(
            {"hephaestus/a.py::B607": 1}, {"hephaestus/a.py::B607": 1}, [], id="unchanged"
        ),
        pytest.param(
            {"hephaestus/a.py::B607": 2},
            {"hephaestus/a.py::B607": 1},
            ["REGRESSION: hephaestus/a.py::B607 count increased (1 -> 2)"],
            id="increased-count",
        ),
        pytest.param(
            {"hephaestus/b.py::B999": 1},
            {"hephaestus/a.py::B999": 1},
            [
                "STALE BASELINE: hephaestus/a.py::B999 is no longer observed (1 -> 0)",
                "REGRESSION: hephaestus/b.py::B999 is new (0 -> 1)",
            ],
            id="per-file-offset-cannot-cancel-across-files",
        ),
        pytest.param(
            {},
            {"hephaestus/a.py::B607": 1},
            ["STALE BASELINE: hephaestus/a.py::B607 is no longer observed (1 -> 0)"],
            id="removed-finding",
        ),
    ],
)
def test_diff_files_against_baseline_classifies(
    current: dict[str, int],
    baseline: dict[str, int],
    expected: list[str],
) -> None:
    """The per-file comparator classifies changes without cross-file cancellation."""
    assert diff_files_against_baseline(current, baseline) == expected


def test_main_uses_files_section_when_baseline_provides_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A files section in the baseline turns on per-file drift enforcement."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "errors": [],
                "results": [
                    {"test_id": "B999", "issue_severity": "LOW", "filename": "hephaestus/x.py"}
                ],
            }
        ),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(
            {
                "generated_by": "issue #3593",
                "severity": "LOW",
                "counts": {"B999": 1},
                "files": {"hephaestus/y.py::B999": 1},
            }
        ),
        encoding="utf-8",
    )

    assert main([str(report_path), str(baseline_path)]) == 1
    output = capsys.readouterr().out
    assert "REGRESSION: hephaestus/x.py::B999 is new" in output
    assert "STALE BASELINE: hephaestus/y.py::B999 is no longer observed" in output


def test_main_legacy_baseline_without_files_section_ignores_file_drift(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Totals-only baselines compare exactly as they did before per-file support."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "errors": [],
                "results": [
                    {"test_id": "B999", "issue_severity": "LOW", "filename": "hephaestus/x.py"}
                ],
            }
        ),
        encoding="utf-8",
    )
    baseline_path.write_text(
        json.dumps(
            {
                "generated_by": "issue #3593",
                "severity": "LOW",
                "counts": {"B999": 1},
            }
        ),
        encoding="utf-8",
    )

    assert main([str(report_path), str(baseline_path)]) == 0
    assert capsys.readouterr().out == ""


def test_main_update_baseline_writes_files_section(
    tmp_path: Path,
) -> None:
    """--update-baseline persists both the totals and the per-file section."""
    report_path = tmp_path / "report.json"
    baseline_path = tmp_path / "baseline.json"
    report_path.write_text(
        json.dumps(
            {
                "errors": [],
                "results": [
                    {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
                    {"test_id": "B607", "issue_severity": "LOW", "filename": "hephaestus/a.py"},
                    {"test_id": "B311", "issue_severity": "LOW", "filename": "hephaestus/b.py"},
                ],
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
                "#1",
            ]
        )
        == 0
    )
    document = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert document["counts"] == {"B311": 1, "B607": 2}
    assert document["files"] == {
        "hephaestus/a.py::B607": 2,
        "hephaestus/b.py::B311": 1,
    }
