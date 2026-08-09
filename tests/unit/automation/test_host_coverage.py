"""Tests for the shell-free immutable-review coverage runner."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import call, patch

import pytest

from hephaestus.automation import host_coverage


def test_main_runs_policy_only_after_unit_coverage_passes() -> None:
    """The policy check consumes the report only after pytest creates it."""
    with patch.object(host_coverage, "_run", side_effect=[0, 0]) as run:
        assert host_coverage.main() == 0

    assert run.call_args_list == [
        call(host_coverage._UNIT_COVERAGE_ARGS),
        call(host_coverage._COVERAGE_POLICY_ARGS),
    ]


def test_main_stops_when_unit_coverage_fails() -> None:
    """A failed test command cannot be hidden by a later policy command."""
    with patch.object(host_coverage, "_run", return_value=7) as run:
        assert host_coverage.main() == 7

    run.assert_called_once_with(host_coverage._UNIT_COVERAGE_ARGS)


def test_run_uses_current_interpreter_and_preserves_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shell-free runner forwards both output streams and the return code."""
    result = subprocess.CompletedProcess(
        args=[], returncode=4, stdout="standard output\n", stderr="standard error\n"
    )
    with patch.object(host_coverage, "run_subprocess", return_value=result) as run:
        assert host_coverage._run(("-m", "example")) == 4

    run.assert_called_once_with([sys.executable, "-m", "example"], check=False)
    captured = capsys.readouterr()
    assert captured.out == "standard output\n"
    assert captured.err == "standard error\n"
