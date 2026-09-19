"""Tests for hephaestus.validation.doc_config."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.utils.helpers import NETWORK_TIMEOUT
from hephaestus.validation.doc_config import (
    _format_consistency_error,
    check_addopts_cov_fail_under,
    check_agents_md_threshold,
    check_claude_md_threshold,
    check_doc_config_consistency,
    check_dod_threshold,
    check_readme_cov_path,
    check_readme_test_count,
    collect_actual_test_count,
    extract_cov_fail_under_from_addopts,
    extract_cov_path,
    load_coverage_threshold,
    main,
)


@pytest.mark.parametrize(
    "message",
    [
        "AGENTS.md: No coverage threshold mention found (expected pattern: '<N>%+ test coverage')",
        (
            "DEFINITION_OF_DONE.md: No coverage threshold mention found "
            "(expected '--cov-fail-under=<N>' or 'drops total under <N>%')"
        ),
    ],
)
def test_human_fallback_keeps_single_percent_sign(message: str) -> None:
    """Keep literal percent signs unchanged in English fallback text."""
    assert _format_consistency_error(message) == message


def test_main_json_reports_missing_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Machine mode emits one JSON error when project metadata is absent."""
    monkeypatch.setattr(
        "sys.argv",
        ["check-doc-config", "--repo-root", str(tmp_path), "--json", "--skip-test-count"],
    )

    assert main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["passed"] is False
    assert payload["error"] == "configuration_error"


def _write_pyproject(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(content)
    return p


def _write_nightly_coverage_workflow(tmp_path: Path, coverage_path: str) -> Path:
    """Write the nightly unit-coverage command used as the coverage source."""
    workflow_path = tmp_path / ".github" / "workflows" / "nightly-tests.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text(
        """jobs:
  unit-coverage:
    steps:
      - name: Run full unit coverage
        run: uv run pytest tests/unit --cov="""
        + coverage_path
        + "\n"
    )
    return workflow_path


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

    def test_invalid_toml_exits_with_parse_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_pyproject(tmp_path, "[tool.coverage\n")

        with pytest.raises(SystemExit) as exc:
            load_coverage_threshold(tmp_path)

        assert exc.value.code == 1
        assert "Could not parse" in capsys.readouterr().err


class TestExtractCovPath:
    """Tests for extract_cov_path()."""

    def test_reads_cov_path_from_nightly_workflow(self, tmp_path: Path) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(addopts='addopts = ["-m", "precommit"]'),
        )
        _write_nightly_coverage_workflow(tmp_path, "mypackage")
        assert extract_cov_path(tmp_path) == "mypackage"

    def test_missing_cov_flag_exits(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(addopts='addopts = ["-v"]'))
        _write_nightly_coverage_workflow(tmp_path, "")
        with pytest.raises(SystemExit) as exc:
            extract_cov_path(tmp_path)
        assert exc.value.code == 1

    def test_invalid_workflow_yaml_exits_with_parse_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow_path = tmp_path / ".github" / "workflows" / "nightly-tests.yml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text("jobs: [\n")

        with pytest.raises(SystemExit) as exc:
            extract_cov_path(tmp_path)

        assert exc.value.code == 1
        assert "Could not parse" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "workflow",
        [
            "{}\n",
            "jobs: []\n",
            "jobs:\n  unit-coverage: []\n",
            "jobs:\n  unit-coverage:\n    steps: {}\n",
            "jobs:\n  unit-coverage:\n    steps:\n      - not-a-mapping\n",
            "jobs:\n  unit-coverage:\n    steps:\n      - run: 123\n",
        ],
        ids=[
            "missing-jobs",
            "jobs-not-mapping",
            "coverage-job-not-mapping",
            "steps-not-list",
            "step-not-mapping",
            "run-not-string",
        ],
    )
    def test_invalid_workflow_shapes_exit_without_cov_path(
        self, tmp_path: Path, workflow: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        workflow_path = tmp_path / ".github" / "workflows" / "nightly-tests.yml"
        workflow_path.parent.mkdir(parents=True)
        workflow_path.write_text(workflow)

        with pytest.raises(SystemExit) as exc:
            extract_cov_path(tmp_path)

        assert exc.value.code == 1
        assert "No --cov=<path> found" in capsys.readouterr().err


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


class TestCheckAgentsMdThreshold:
    """Tests for check_agents_md_threshold()."""

    def test_matching_threshold(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        assert check_agents_md_threshold(tmp_path, 80) == []

    def test_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("We maintain 75%+ test coverage.")
        errors = check_agents_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "75%" in errors[0]
        assert "80%" in errors[0]

    def test_no_mention(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("No coverage info here.")
        errors = check_agents_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "No coverage threshold" in errors[0]

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = check_agents_md_threshold(tmp_path, 80)
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_percent_without_plus(self, tmp_path: Path) -> None:
        (tmp_path / "AGENTS.md").write_text("We require 80% test coverage.")
        assert check_agents_md_threshold(tmp_path, 80) == []

    def test_legacy_name_delegates(self, tmp_path: Path) -> None:
        """The old helper name remains a working compatibility alias."""
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        assert check_claude_md_threshold(tmp_path, 80) == []


class TestCheckDodThreshold:
    """Tests for check_dod_threshold()."""

    def _write_dod(self, tmp_path: Path, text: str) -> None:
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(text)

    def test_matching_threshold(self, tmp_path: Path) -> None:
        self._write_dod(tmp_path, "`--cov-fail-under=83` and drops total under 83%.")
        assert check_dod_threshold(tmp_path, 83) == []

    def test_mismatch_cov_fail_under(self, tmp_path: Path) -> None:
        self._write_dod(tmp_path, "`--cov-fail-under=80`")
        errors = check_dod_threshold(tmp_path, 83)
        assert len(errors) == 1
        assert "80%" in errors[0]
        assert "83%" in errors[0]

    def test_mismatch_drops_under(self, tmp_path: Path) -> None:
        self._write_dod(tmp_path, "drops total under 80%")
        errors = check_dod_threshold(tmp_path, 83)
        assert len(errors) == 1
        assert "80%" in errors[0]

    def test_no_mention(self, tmp_path: Path) -> None:
        self._write_dod(tmp_path, "No coverage info here.")
        errors = check_dod_threshold(tmp_path, 83)
        assert len(errors) == 1
        assert "No coverage threshold" in errors[0]

    def test_missing_file(self, tmp_path: Path) -> None:
        errors = check_dod_threshold(tmp_path, 83)
        assert len(errors) == 1
        assert "not found" in errors[0]


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

    def test_pytest_collection_passes_timeout(self, tmp_path: Path) -> None:
        """The pytest --collect-only subprocess is bounded (#684)."""
        with patch("hephaestus.validation.doc_config.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="5 tests collected\n", stderr="")
            collect_actual_test_count(tmp_path)
        assert mock_run.call_args.kwargs["timeout"] == NETWORK_TIMEOUT

    def test_returns_none_on_timeout(self, tmp_path: Path) -> None:
        """A hung pytest collection degrades to None instead of hanging (#684)."""
        with patch(
            "hephaestus.validation.doc_config.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pytest", timeout=120),
        ):
            assert collect_actual_test_count(tmp_path) is None


class TestCheckDocConfigConsistency:
    """Tests for check_doc_config_consistency()."""

    def _setup_valid_repo(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=80))
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus")
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(
            "`--cov-fail-under=80` and drops total under 80%."
        )

    def test_all_pass(self, tmp_path: Path) -> None:
        self._setup_valid_repo(tmp_path)
        result = check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert result == 0

    def test_threshold_mismatch_fails(self, tmp_path: Path) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=90))
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("")
        result = check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert result == 1

    def test_dod_threshold_mismatch_fails(
        self, tmp_path: Path, capsys: pytest.CaptureFixture
    ) -> None:
        """A stale threshold in DEFINITION_OF_DONE.md fails the consolidated run."""
        self._setup_valid_repo(tmp_path)
        # Rewrite the DoD with a threshold that no longer matches fail_under=80.
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(
            "`--cov-fail-under=83` and drops total under 83%."
        )
        result = check_doc_config_consistency(tmp_path, skip_test_count=True)
        assert result == 1
        assert "DEFINITION_OF_DONE.md" in capsys.readouterr().err

    @pytest.mark.parametrize(
        ("case", "expected_message"),
        [
            ("missing-agents", "AGENTS.md not found"),
            ("missing-dod", "DEFINITION_OF_DONE.md not found"),
            ("missing-readme", "README.md not found"),
            ("readme-cov-mismatch", "--cov path mismatch"),
            ("addopts-mismatch", "--cov-fail-under mismatch"),
            ("test-count-mismatch", "Test count mismatch"),
        ],
    )
    def test_reports_public_consistency_error_formats(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        case: str,
        expected_message: str,
    ) -> None:
        self._setup_valid_repo(tmp_path)
        skip_test_count = True

        if case == "missing-agents":
            (tmp_path / "AGENTS.md").unlink()
        elif case == "missing-dod":
            (tmp_path / "docs" / "DEFINITION_OF_DONE.md").unlink()
        elif case == "missing-readme":
            (tmp_path / "README.md").unlink()
        elif case == "readme-cov-mismatch":
            (tmp_path / "README.md").write_text("Run pytest --cov=other-package")
        elif case == "addopts-mismatch":
            _write_pyproject(
                tmp_path,
                _minimal_pyproject(
                    fail_under=80,
                    addopts='addopts = ["--cov=hephaestus", "--cov-fail-under=70"]',
                ),
            )
        else:
            (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus\nWe have 1 tests.")
            monkeypatch.setattr(
                "hephaestus.validation.doc_config.collect_actual_test_count",
                lambda _repo_root: 100,
            )
            skip_test_count = False

        assert check_doc_config_consistency(tmp_path, skip_test_count=skip_test_count) == 1
        assert expected_message in capsys.readouterr().err

    def test_verbose_reports_matching_addopts_threshold(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write_pyproject(
            tmp_path,
            _minimal_pyproject(
                fail_under=80,
                addopts='addopts = ["--cov=hephaestus", "--cov-fail-under=80"]',
            ),
        )
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus")
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(
            "`--cov-fail-under=80` and drops total under 80%."
        )

        assert check_doc_config_consistency(tmp_path, verbose=True, skip_test_count=True) == 0
        assert "PASS: addopts --cov-fail-under matches" in capsys.readouterr().out

    def test_verbose_reports_unavailable_test_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._setup_valid_repo(tmp_path)
        monkeypatch.setattr(
            "hephaestus.validation.doc_config.collect_actual_test_count",
            lambda _repo_root: None,
        )

        assert check_doc_config_consistency(tmp_path, verbose=True) == 0
        assert "SKIP: Could not collect actual test count" in capsys.readouterr().out

    def test_verbose_reports_matching_test_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._setup_valid_repo(tmp_path)
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus\nWe have 100 tests.")
        monkeypatch.setattr(
            "hephaestus.validation.doc_config.collect_actual_test_count",
            lambda _repo_root: 100,
        )

        assert check_doc_config_consistency(tmp_path, verbose=True) == 0
        assert "PASS: README.md test count is within" in capsys.readouterr().out

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
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus")
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(
            "`--cov-fail-under=80` and drops total under 80%."
        )
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

    def test_json_reports_matching_test_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=80))
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
        (tmp_path / "README.md").write_text("Run pytest --cov=hephaestus\nWe have 100 tests.")
        (tmp_path / "docs").mkdir(exist_ok=True)
        (tmp_path / "docs" / "DEFINITION_OF_DONE.md").write_text(
            "`--cov-fail-under=80` and drops total under 80%."
        )
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-doc-config", "--repo-root", str(tmp_path), "--json"],
        )
        monkeypatch.setattr(
            "hephaestus.validation.doc_config.collect_actual_test_count",
            lambda _repo_root: 100,
        )

        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload == {
            "errors": [],
            "exit_code": 0,
            "expected_threshold": 80,
            "passed": True,
        }

    def test_mismatch_fails(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _write_pyproject(tmp_path, _minimal_pyproject(fail_under=90))
        _write_nightly_coverage_workflow(tmp_path, "hephaestus")
        (tmp_path / "AGENTS.md").write_text("We maintain 80%+ test coverage.")
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
