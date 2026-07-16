"""Tests for the pre-commit benchmark helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.ci.precommit import (
    bench_precommit_main,
    check_threshold,
    emit_warning,
    format_summary_table,
    load_precommit_config,
    write_step_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_summary_table_includes_reported_values() -> None:
    """The benchmark report preserves its measured status and counts."""
    table = format_summary_table(45, 300, "passed")
    assert "passed" in table
    assert "45s" in table
    assert "300" in table


def test_threshold_is_strictly_greater_than_limit() -> None:
    """A run exactly at the threshold remains within the timing budget."""
    assert check_threshold(120, 120) is False
    assert check_threshold(121, 120) is True


def test_warning_uses_github_actions_annotation(capsys: pytest.CaptureFixture[str]) -> None:
    """A slow run emits an annotation consumable by GitHub Actions."""
    emit_warning("slow hooks")
    assert "::warning::slow hooks" in capsys.readouterr().out


def test_summary_is_appended_to_configured_file(tmp_path: Path) -> None:
    """A supplied step-summary path receives the benchmark result."""
    summary_path = tmp_path / "summary.md"
    write_step_summary("report\n", str(summary_path))
    assert summary_path.read_text() == "report\n"


def test_benchmark_cli_reports_json(capsys: pytest.CaptureFixture[str]) -> None:
    """The command exposes a machine-readable timing result."""
    assert bench_precommit_main(["--elapsed", "45", "--files", "3", "--json"]) == 0
    assert '"over_threshold": false' in capsys.readouterr().out


def test_mypy_hook_checks_the_configured_source_tree() -> None:
    """The system hook needs explicit roots because mypy has no default target."""
    repositories = load_precommit_config(REPO_ROOT / ".pre-commit-config.yaml")
    hook = None
    for repository in repositories:
        hooks = repository.get("hooks")
        if not isinstance(hooks, list):
            continue
        for candidate in hooks:
            if isinstance(candidate, dict) and candidate.get("id") == "mypy-check-python":
                hook = candidate
                break
        if hook is not None:
            break

    assert hook is not None
    assert hook["entry"] == "uv run mypy hephaestus/ scripts/ tests/"
    assert hook["pass_filenames"] is False
