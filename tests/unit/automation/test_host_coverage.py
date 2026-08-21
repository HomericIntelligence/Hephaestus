"""Tests for the shell-free immutable-review coverage runner."""

from __future__ import annotations

import subprocess
import sys
from unittest.mock import ANY, call, patch

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


def test_run_uses_current_interpreter_and_bounds_failure_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The runner forwards bounded failure tails and the return code."""
    failure = "FAILED tests/unit/test_example.py::test_failure - assertion failed"
    stdout = failure + "\n" + "x" * (host_coverage._FAILURE_OUTPUT_TAIL_CHARS + 1)
    stderr = "y" * (host_coverage._FAILURE_OUTPUT_TAIL_CHARS + 1)
    result = subprocess.CompletedProcess(args=[], returncode=4, stdout=stdout, stderr=stderr)
    with patch.object(host_coverage, "run_subprocess", return_value=result) as run:
        assert host_coverage._run(("-m", "example")) == 4

    run.assert_called_once_with([sys.executable, "-m", "example"], env=ANY, check=False)
    captured = capsys.readouterr()
    assert captured.out == stdout[-host_coverage._FAILURE_OUTPUT_TAIL_CHARS :]
    assert captured.err == (
        stderr[-host_coverage._FAILURE_OUTPUT_TAIL_CHARS :]
        + "\nHost coverage failure index:\n"
        + failure
        + "\n"
    )


def test_run_is_silent_on_success(capsys: pytest.CaptureFixture[str]) -> None:
    """Successful verbose suites cannot exhaust the verifier receipt channel."""
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="verbose", stderr="warning")
    with patch.object(host_coverage, "run_subprocess", return_value=result):
        assert host_coverage._run(("-m", "example")) == 0

    assert capsys.readouterr() == ("", "")
