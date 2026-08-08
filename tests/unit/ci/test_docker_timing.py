"""Tests for hephaestus.ci.docker_timing."""

from __future__ import annotations

from hephaestus.ci.docker_timing import (
    build_summary_table,
    compute_reduction,
    count_cached_layers,
)


class TestCountCachedLayers:
    """Tests for count_cached_layers()."""

    def test_no_cached(self) -> None:
        assert count_cached_layers("") == 0

    def test_single_cached(self) -> None:
        assert count_cached_layers("#1 CACHED") == 1

    def test_multiple_cached(self) -> None:
        log = "#1 CACHED\n#2 CACHED\n#3 [1/5] FROM ...\n#4 CACHED"
        assert count_cached_layers(log) == 3

    def test_case_insensitive(self) -> None:
        assert count_cached_layers("cached CACHED CaChEd") == 3


class TestComputeReduction:
    """Tests for compute_reduction()."""

    def test_50_percent(self) -> None:
        assert compute_reduction(100, 50) == 50.0

    def test_zero_cold_returns_zero(self) -> None:
        assert compute_reduction(0, 10) == 0.0

    def test_negative_cold_returns_zero(self) -> None:
        assert compute_reduction(-5, 10) == 0.0

    def test_rounds_to_one_decimal(self) -> None:
        result = compute_reduction(300, 199)
        assert result == round((300 - 199) / 300 * 100, 1)

    def test_full_reduction(self) -> None:
        assert compute_reduction(100, 0) == 100.0

    def test_no_reduction(self) -> None:
        assert compute_reduction(100, 100) == 0.0


class TestBuildSummaryTable:
    """Tests for build_summary_table()."""

    def test_contains_header(self) -> None:
        table = build_summary_table(120, 60, 10, 50.0)
        assert "Docker Build Timing" in table

    def test_pass_verdict(self) -> None:
        table = build_summary_table(100, 50, 5, 50.0)
        assert "PASS" in table

    def test_fail_verdict(self) -> None:
        table = build_summary_table(100, 80, 2, 20.0)
        assert "FAIL" in table

    def test_contains_values(self) -> None:
        table = build_summary_table(120, 60, 8, 50.0)
        assert "120s" in table
        assert "60s" in table
        assert "50.0%" in table
        assert "8" in table

    def test_custom_threshold(self) -> None:
        # reduction=20%, threshold=15% → PASS
        table = build_summary_table(100, 80, 3, 20.0, acceptance_threshold=15.0)
        assert "PASS" in table

    def test_custom_threshold_fail(self) -> None:
        # reduction=20%, threshold=25% → FAIL
        table = build_summary_table(100, 80, 3, 20.0, acceptance_threshold=25.0)
        assert "FAIL" in table
