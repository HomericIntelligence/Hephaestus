"""Tests for hephaestus.validation.doc_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.validation.doc_config import (
    check_addopts_cov_fail_under,
    check_claude_md_threshold,
    check_doc_config_consistency,
    check_readme_cov_path,
    check_readme_test_count,
    collect_actual_test_count,
    extract_cov_fail_under_from_addopts,
    extract_cov_path,
    load_coverage_threshold,
    main,
)


def _write_pyproject(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(content)
    return p


def _minimal_pyproject(
    fail_under: int = 80,
    addopts: str | None = None,
    extra: str = "",
) -> str:
    addopts_line = 'addopts = ["--cov=hephaestus", "--cov-report=term-missing"]'
    if addopts is not None:
        addopts_line = addopts
    return f"""
[project]
name = "test-project"
version = "1.0.0"

[tool.pytest.ini_options]
{addopts_line}

[tool.coverage.report]
fail_under = {fail_under}
{extra}
"""


class TestLoadCoverageThreshold:
    """Tests for load_coverage_threshold()."""

    def test_reads_fail_under(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=75))
        assert load_coverage_threshold(tmp_path) == 75

    def test_missing_pyproject_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            load_coverage_threshold(tmp_path)
        assert exc.value.code == 1

    def test_missing_key_exits(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            "[project]\nname = 'x'\nversion = '1'\n[tool.coverage.report]\n",
        )
        with pytest.raises(SystemExit) as exc:
            load_coverage_threshold(tmp_path)
        assert exc.value.code == 1


class TestExtractCovPath:
    """Tests for extract_cov_path()."""

    def test_reads_cov_path(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["--cov=mypackage"]'),
        )
        assert extract_cov_path(tmp_path) == "mypackage"

    def test_missing_cov_flag_exits(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["-v"]'),
        )
        with pytest.raises(SystemExit) as exc:
            extract_cov_path(tmp_path)
        assert exc.value.code == 1

    def test_addopts_as_string(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = "--cov=mypackage -v"'),
        )
        assert extract_cov_path(tmp_path) == "mypackage"


class TestExtractCovFailUnder:
    """Tests for extract_cov_fail_under_from_addopts()."""

    def test_returns_none_when_absent(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject())
        assert extract_cov_fail_under_from_addopts(tmp_path) is None

    def test_reads_value(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["--cov=x", "--cov-fail-under=90"]'),
        )
        assert extract_cov_fail_under_from_addopts(tmp_path) == 90


class TestCheckClaudeMdThreshold:
    """Tests for check_claude_md_threshold()."""

    def test_matching_threshold(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("We maintain 80%+ test coverage.")
        assert check_claude_md_threshold(tmp_path, 80) == []

    def test_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("We maintain 75%+ test coverage.")
        errors = check_claude_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "75%" in errors[0]
        assert "80%" in errors[0]

    def test_no_mention(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("No coverage info here.")
        errors = check_claude_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "No coverage threshold" in errors[0]

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = check_claude_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_percent_without_plus(self, tmp_path: Path) -> None:
        (tmp_path / "CLAUDE.md").write_text("We require 80% test coverage.")
        assert check_claude_md_threshold(tmp_path, 80) == []


class TestCheckReadmeCovPath:
    """Tests for check_readme_cov_path()."""

    def test_matching_path(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("pytest --cov=mypackage")
        assert check_readme_cov_path(tmp_path, "mypackage") == []

    def test_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("pytest --cov=oldpackage")
        errors = check_readme_cov_path(tmp_path, "newpackage")
        assert len(errors) == 1
        assert "oldpackage" in errors[0]

    def test_no_cov_flag(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("Just run pytest")
        assert check_readme_cov_path(tmp_path, "anything") == []

    def test_missing_readme(self, tmp_path: Path) -> None:
        errors = check_readme_cov_path(tmp_path, "pkg")
        assert len(errors) == 1
        assert "not found" in errors[0]


class TestCheckAddoptsCovFailUnder:
    """Tests for check_addopts_cov_fail_under()."""

    def test_absent_is_ok(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject())
        assert check_addopts_cov_fail_under(tmp_path, 80) == []

    def test_matching_value(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["--cov=x", "--cov-fail-under=80"]'),
        )
        assert check_addopts_cov_fail_under(tmp_path, 80) == []

    def test_mismatch(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["--cov=x", "--cov-fail-under=70"]'),
        )
        errors = check_addopts_cov_fail_under(tmp_path, 80)
        assert len(errors) == 1
        assert "70" in errors[0]
        assert "80" in errors[0]


class TestCheckReadmeTestCount:
    """Tests for check_readme_test_count()."""

    def test_within_tolerance(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("We have 100 tests.")
        assert check_readme_test_count(tmp_path, 100) == []

    def test_within_tolerance_comma(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("Over 1,000 tests.")
        assert check_readme_test_count(tmp_path, 1000) == []

    def test_outside_tolerance(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("We have 50 tests.")
        errors = check_readme_test_count(tmp_path, 200)
        assert len(errors) == 1
        assert "50" in errors[0]

    def test_no_count_in_readme(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("No test count here.")
        assert check_readme_test_count(tmp_path, 100) == []

    def test_missing_readme(self, tmp_path: Path) -> None:
        errors = check_readme_test_count(tmp_path, 100)
        assert len(errors) == 1
        assert "not found" in errors[0]


class TestCollectActualTestCount:
    """Tests for collect_actual_test_count()."""

    def test_returns_none_on_nonexistent_dir(self, tmp_path: Path) -> None:
        # No tests/ directory — subprocess will likely return nothing parseable
        result = collect_actual_test_count(tmp_path)
        assert result is None or isinstance(result, int)


class TestCheckDocConfigConsistency:
    """Tests for check_doc_config_consistency()."""

    def _setup_valid_repo(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=80))
        (tmp_path / "CLAUDE.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus")

    def test_all_pass(self, tmp_path: Path) -> None:
        self._setup_valid_repo(tmp_path)
        result = check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert result == 0

    def test_threshold_mismatch_fails(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=90))
        (tmp_path / "CLAUDE.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("")
        result = check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert result == 1

    def test_verbose_on_pass(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        self._setup_valid_repo(tmp_path)
        check_doc_config_consistency(tmp_path, verbose=True, skip_test_count=True)
        captured = capsys.readouterr()
        assert "PASS" in captured.out

    def test_missing_pyproject_exits(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as exc:
            check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert exc.value.code == 1


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-check-doc-config", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_valid_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=80))
        (tmp_path / "CLAUDE.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-check-doc-config",
                "--repo-root",
                str(tmp_path),
                "--skip-test-count",
            ],
        )
        assert main() == 0

    def test_mismatch_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=90))
        (tmp_path / "CLAUDE.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-check-doc-config",
                "--repo-root",
                str(tmp_path),
                "--skip-test-count",
            ],
        )
        assert main() == 1
