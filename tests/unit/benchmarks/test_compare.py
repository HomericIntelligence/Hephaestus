#!/usr/bin/env python3

"""Tests for hephaestus.benchmarks.compare module."""

import json
from pathlib import Path
from typing import Any

import pytest

from hephaestus.benchmarks.compare import (
    Regression,
    detect_regressions,
    extract_timings,
    format_markdown_report,
    load_benchmark_results,
)


class TestLoadBenchmarkResults:
    """Tests for load_benchmark_results."""

    def test_load_benchmark_results(self, tmp_path: Path) -> None:
        """Test loading benchmark results from JSON."""
        results_file = tmp_path / "results.json"
        data = {"benchmarks": [{"name": "test1", "duration_ms": 100.0}]}
        results_file.write_text(json.dumps(data))

        results = load_benchmark_results(results_file)
        assert results == data

    def test_load_benchmark_results_file_not_found(self) -> None:
        """Test loading non-existent file raises error."""
        with pytest.raises(FileNotFoundError):
            load_benchmark_results(Path("nonexistent.json"))


class TestExtractTimings:
    """Tests for extract_timings."""

    def test_extract_timings(self) -> None:
        """Test extracting timings from benchmark results."""
        results = {
            "benchmarks": [
                {"name": "bench1", "duration_ms": 100.5},
                {"name": "bench2", "duration_ms": 200.75},
                {"name": "bench3", "duration_ms": 50.0},
            ]
        }

        timings = extract_timings(results)

        assert len(timings) == 3
        assert timings["bench1"] == 100.5
        assert timings["bench2"] == 200.75
        assert timings["bench3"] == 50.0

    def test_extract_timings_empty(self) -> None:
        """Test extracting timings from empty results."""
        results: dict[str, Any] = {"benchmarks": []}
        timings = extract_timings(results)
        assert timings == {}

    def test_extract_timings_missing_data(self) -> None:
        """Test extracting timings with missing data."""
        results = {
            "benchmarks": [
                {"name": "bench1", "duration_ms": 100.0},
                {"name": "bench2"},  # Missing duration_ms
                {"duration_ms": 200.0},  # Missing name
            ]
        }

        timings = extract_timings(results)
        assert len(timings) == 1
        assert timings["bench1"] == 100.0


class TestDetectRegressions:
    """Tests for detect_regressions."""

    def test_detect_regressions_critical(self) -> None:
        """Test detecting critical regressions (>25%)."""
        current = {"bench1": 130.0}  # 30% slower
        baseline = {"bench1": 100.0}

        regressions, improvements = detect_regressions(current, baseline)

        assert len(regressions) == 1
        assert regressions[0].severity == "critical"
        assert regressions[0].change_percent > 25.0
        assert len(improvements) == 0

    def test_detect_regressions_high(self) -> None:
        """Test detecting high severity regressions (10-25%)."""
        current = {"bench1": 115.0}  # 15% slower
        baseline = {"bench1": 100.0}

        regressions, _improvements = detect_regressions(current, baseline)

        assert len(regressions) == 1
        assert regressions[0].severity == "high"
        assert 10.0 < regressions[0].change_percent < 25.0

    def test_detect_regressions_medium(self) -> None:
        """Test detecting medium severity regressions (5-10%)."""
        current = {"bench1": 107.0}  # 7% slower
        baseline = {"bench1": 100.0}

        regressions, _improvements = detect_regressions(current, baseline)

        assert len(regressions) == 1
        assert regressions[0].severity == "medium"
        assert 5.0 < regressions[0].change_percent < 10.0

    def test_detect_regressions_improvements(self) -> None:
        """Test detecting improvements (>5% faster)."""
        current = {"bench1": 90.0}  # 10% faster
        baseline = {"bench1": 100.0}

        regressions, improvements = detect_regressions(current, baseline)

        assert len(regressions) == 0
        assert len(improvements) == 1
        assert improvements[0]["improvement_percent"] == 10.0

    def test_detect_regressions_no_change(self) -> None:
        """Test benchmarks with no significant change."""
        current = {"bench1": 102.0}  # 2% slower (below threshold)
        baseline = {"bench1": 100.0}

        regressions, improvements = detect_regressions(current, baseline)

        assert len(regressions) == 0
        assert len(improvements) == 0

    def test_detect_regressions_new_benchmark(self) -> None:
        """Test handling new benchmarks not in baseline."""
        current = {"bench1": 100.0, "bench2": 200.0}
        baseline = {"bench1": 100.0}

        regressions, improvements = detect_regressions(current, baseline)

        # New benchmark (bench2) should be ignored
        assert len(regressions) == 0
        assert len(improvements) == 0

    def test_detect_regressions_custom_thresholds(self) -> None:
        """Test using custom thresholds."""
        current = {"bench1": 112.0}  # 12% slower
        baseline = {"bench1": 100.0}

        # With default thresholds, this is "high"
        regressions, _ = detect_regressions(current, baseline)
        assert regressions[0].severity == "high"

        # With custom threshold at 15%, this should be "medium"
        regressions, _ = detect_regressions(
            current, baseline, critical_threshold=30.0, high_threshold=15.0, medium_threshold=5.0
        )
        assert regressions[0].severity == "medium"


class TestFormatMarkdownReport:
    """Tests for format_markdown_report."""

    def test_format_markdown_report_with_regressions(self) -> None:
        """Test generating markdown report with regressions."""
        regressions = [
            Regression(
                benchmark="bench1",
                baseline_ms=100.0,
                current_ms=130.0,
                change_percent=30.0,
                severity="critical",
            )
        ]
        improvements: list[dict[str, Any]] = []
        current_results: dict[str, Any] = {
            "environment": {
                "os": "Linux",
                "cpu": "AMD Ryzen",
                "runtime_version": "Python 3.11",
                "git_commit": "abc123",
            }
        }
        baseline_results: dict[str, Any] = {"environment": {}}

        report = format_markdown_report(
            regressions, improvements, current_results, baseline_results
        )

        assert "# Performance Regression Report" in report
        assert "1 CRITICAL regressions detected" in report
        assert "bench1" in report
        assert "30.0%" in report
        assert "Linux" in report
        assert "AMD Ryzen" in report

    def test_format_markdown_report_with_improvements(self) -> None:
        """Test generating markdown report with improvements."""
        regressions: list[Regression] = []
        improvements: list[dict[str, Any]] = [
            {
                "benchmark": "bench1",
                "baseline_ms": 100.0,
                "current_ms": 80.0,
                "improvement_percent": 20.0,
            }
        ]
        current_results: dict[str, Any] = {"environment": {}}
        baseline_results: dict[str, Any] = {"environment": {}}

        report = format_markdown_report(
            regressions, improvements, current_results, baseline_results
        )

        assert "# Performance Regression Report" in report
        assert "1 improvements detected" in report
        assert "bench1" in report
        assert "20.0%" in report

    def test_format_markdown_report_no_changes(self) -> None:
        """Test generating markdown report with no changes."""
        regressions: list[Regression] = []
        improvements: list[dict[str, Any]] = []
        current_results: dict[str, Any] = {"environment": {}}
        baseline_results: dict[str, Any] = {"environment": {}}

        report = format_markdown_report(
            regressions, improvements, current_results, baseline_results
        )

        assert "# Performance Regression Report" in report
        assert "No significant performance changes detected" in report


class TestRegression:
    """Tests for the Regression dataclass."""

    def test_regression_dataclass(self) -> None:
        """Test Regression dataclass."""
        reg = Regression(
            benchmark="test",
            baseline_ms=100.0,
            current_ms=150.0,
            change_percent=50.0,
            severity="critical",
        )

        assert reg.benchmark == "test"
        assert reg.baseline_ms == 100.0
        assert reg.current_ms == 150.0
        assert reg.change_percent == 50.0
        assert reg.severity == "critical"
