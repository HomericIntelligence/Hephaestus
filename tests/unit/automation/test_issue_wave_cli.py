"""CLI and configuration coverage for staged issue waves."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import loop_runner
from hephaestus.automation.loop_runner import (
    LoopConfig,
    _build_pipeline_config,
    _parse_args,
    _source_revision,
)


@pytest.mark.parametrize(
    ("flag", "value"),
    [("--loops", "0"), ("--parallel-repos", "-1"), ("--issue-limit", "0")],
)
def test_wave_numeric_flags_are_positive(flag: str, value: str) -> None:
    """All wave-related numeric controls reject non-positive values."""
    with pytest.raises(SystemExit):
        _parse_args([flag, value])


def test_issue_limit_is_mutually_exclusive_with_identifier_scopes() -> None:
    """Explicit issue and PR identifiers retain their recovery semantics."""
    with pytest.raises(SystemExit) as issue_error:
        _parse_args(["--issue-limit", "1", "--issues", "42"])
    assert issue_error.value.code == 2
    with pytest.raises(SystemExit) as pr_error:
        _parse_args(["--issue-limit", "1", "--prs", "43"])
    assert pr_error.value.code == 2


@pytest.mark.parametrize("limit", [1, 2, 4, 8])
def test_positive_wave_limits_parse(limit: int) -> None:
    """The rollout selectors are parsed as values, never as identifiers."""
    args = _parse_args(["--issue-limit", str(limit)])
    assert args.issue_limit == limit


def test_pipeline_config_carries_issue_limit_without_shifting_legacy_fields(
    tmp_path: Path,
) -> None:
    """The public config carries the selector and preserves keyword behavior."""
    args = argparse.Namespace(json=False)
    cfg = LoopConfig(issue_limit=4, projects_dir=tmp_path)
    pipeline = _build_pipeline_config(args, cfg, "acme", ["hephaestus"])
    assert pipeline.issue_limit == 4
    assert pipeline.repo_source_factory is None


def test_source_revision_reads_exact_checkout_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An editable checkout binds the event provenance to its full commit."""
    (tmp_path / ".git").mkdir()
    run_git = Mock(return_value=subprocess.CompletedProcess([], 0, "a" * 40 + "\n", ""))
    monkeypatch.setattr(loop_runner, "run_git", run_git)

    assert _source_revision(tmp_path) == "a" * 40
    run_git.assert_called_once_with(
        ["rev-parse", "--verify", "HEAD"],
        cwd=tmp_path,
        timeout=10,
        log_on_error=False,
    )


def test_source_revision_is_unavailable_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wheel installation does not infer a revision from a parent checkout."""
    run_git = Mock()
    monkeypatch.setattr(loop_runner, "run_git", run_git)

    assert _source_revision(tmp_path) is None
    run_git.assert_not_called()
