"""Tests for hephaestus.ci.precommit."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.ci.precommit import (
    check_threshold,
    check_version_drift,
    emit_warning,
    extract_external_hooks,
    format_summary_table,
    normalize_version,
    parse_pixi_constraint,
    write_step_summary,
)


class TestFormatSummaryTable:
    """Tests for format_summary_table()."""

    def test_passed_status(self) -> None:
        table = format_summary_table(45, 300, "passed")
        assert "passed" in table
        assert "45s" in table
        assert "300" in table

    def test_failed_status(self) -> None:
        table = format_summary_table(10, 5, "failed")
        assert "failed" in table

    def test_contains_header(self) -> None:
        table = format_summary_table(0, 0, "passed")
        assert "Pre-commit Hook Benchmark" in table

    def test_markdown_table_format(self) -> None:
        table = format_summary_table(30, 100, "passed")
        assert "|" in table


class TestCheckThreshold:
    """Tests for check_threshold()."""

    def test_below_threshold(self) -> None:
        assert check_threshold(60, 120) is False

    def test_above_threshold(self) -> None:
        assert check_threshold(150, 120) is True

    def test_equal_threshold_not_slow(self) -> None:
        assert check_threshold(120, 120) is False

    def test_default_threshold(self) -> None:
        assert check_threshold(121) is True
        assert check_threshold(119) is False


class TestEmitWarning:
    """Tests for emit_warning()."""

    def test_outputs_annotation(self, capsys: pytest.CaptureFixture) -> None:
        emit_warning("slow hooks")
        captured = capsys.readouterr()
        assert "::warning::slow hooks" in captured.out


class TestWriteStepSummary:
    """Tests for write_step_summary()."""

    def test_writes_to_path(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.md"
        write_step_summary("## Test\n", str(summary_file))
        assert summary_file.read_text() == "## Test\n"

    def test_appends_to_existing(self, tmp_path: Path) -> None:
        summary_file = tmp_path / "summary.md"
        summary_file.write_text("existing\n")
        write_step_summary("new\n", str(summary_file))
        assert summary_file.read_text() == "existing\nnew\n"

    def test_no_path_no_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
        # Should not raise even with no path set
        write_step_summary("content")


class TestNormalizeVersion:
    """Tests for normalize_version()."""

    def test_strips_v(self) -> None:
        assert normalize_version("v1.19.1") == "1.19.1"

    def test_no_v(self) -> None:
        assert normalize_version("0.7.1") == "0.7.1"

    def test_empty_string(self) -> None:
        assert normalize_version("") == ""


class TestParsePixiConstraint:
    """Tests for parse_pixi_constraint()."""

    def test_gte_range(self) -> None:
        assert parse_pixi_constraint(">=1.19.1,<2") == "1.19.1"

    def test_exact(self) -> None:
        assert parse_pixi_constraint("==0.26.1") == "0.26.1"

    def test_gte_only(self) -> None:
        assert parse_pixi_constraint(">=0.7.1") == "0.7.1"

    def test_bare_version(self) -> None:
        assert parse_pixi_constraint("0.12.1") == "0.12.1"

    def test_unparseable(self) -> None:
        assert parse_pixi_constraint("*") is None


class TestExtractExternalHooks:
    """Tests for extract_external_hooks()."""

    def test_extracts_external_repos(self) -> None:
        repos = [
            {"repo": "https://github.com/pre-commit/mirrors-mypy", "rev": "v1.19.1"},
            {"repo": "local"},
        ]
        result = extract_external_hooks(repos)
        assert "https://github.com/pre-commit/mirrors-mypy" in result
        assert result["https://github.com/pre-commit/mirrors-mypy"] == "v1.19.1"
        assert "local" not in result

    def test_skips_missing_rev(self) -> None:
        repos = [{"repo": "https://github.com/some/tool", "rev": ""}]
        result = extract_external_hooks(repos)
        assert result == {}

    def test_empty_repos(self) -> None:
        assert extract_external_hooks([]) == {}


class TestCheckVersionDrift:
    """Tests for check_version_drift()."""

    def test_no_drift(self) -> None:
        hooks = {"https://github.com/pre-commit/mirrors-mypy": "v1.19.1"}
        pixi = {"mypy": "1.19.1"}
        mapping = {"https://github.com/pre-commit/mirrors-mypy": "mypy"}
        issues = check_version_drift(hooks, pixi, mapping)
        assert issues == []

    def test_drift_detected(self) -> None:
        hooks = {"https://github.com/pre-commit/mirrors-mypy": "v1.20.0"}
        pixi = {"mypy": "1.19.1"}
        mapping = {"https://github.com/pre-commit/mirrors-mypy": "mypy"}
        issues = check_version_drift(hooks, pixi, mapping)
        assert len(issues) == 1
        assert "DRIFT" in issues[0]

    def test_missing_pixi_entry(self) -> None:
        hooks = {"https://github.com/pre-commit/mirrors-mypy": "v1.19.1"}
        mapping = {"https://github.com/pre-commit/mirrors-mypy": "mypy"}
        issues = check_version_drift(hooks, {}, mapping)
        assert len(issues) == 1
        assert "MISSING" in issues[0]

    def test_unmapped_url_skipped(self) -> None:
        hooks = {"https://github.com/some/unmapped-tool": "v1.0.0"}
        issues = check_version_drift(hooks, {}, {})
        assert issues == []


class TestBenchPrecommitMain:
    """Tests for bench_precommit_main() CLI entry point."""

    def test_basic_run(self, capsys: pytest.CaptureFixture) -> None:
        from hephaestus.ci.precommit import bench_precommit_main

        result = bench_precommit_main(["--elapsed", "45", "--files", "100", "--status", "passed"])
        assert result == 0
        captured = capsys.readouterr()
        assert "45s" in captured.out

    def test_slow_emits_warning(self, capsys: pytest.CaptureFixture) -> None:
        from hephaestus.ci.precommit import bench_precommit_main

        result = bench_precommit_main(
            ["--elapsed", "200", "--files", "50", "--status", "passed", "--threshold", "120"]
        )
        assert result == 0
        captured = capsys.readouterr()
        assert "::warning::" in captured.out
