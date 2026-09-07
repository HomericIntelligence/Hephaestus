"""Tests for the shell-free immutable-review coverage runner."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, call, patch

import pytest

from hephaestus.automation import host_coverage
from hephaestus.config.child_environments import build_host_verification_env


@pytest.fixture(autouse=True)
def host_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Supply the host environment for command-output tests."""
    scratch = tmp_path / "scratch"
    environment = build_host_verification_env(
        home=scratch / "home",
        temporary=scratch / "tmp",
        cache=scratch / "cache",
        runtime_environment=tmp_path / "runtime",
        executable=Path(sys.executable),
    )
    monkeypatch.setattr(os, "environ", environment)


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


@pytest.mark.parametrize(
    "args", [host_coverage._UNIT_COVERAGE_ARGS, host_coverage._COVERAGE_POLICY_ARGS]
)
def test_preserves_explicit_scratch_coverage_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, args: tuple[str, ...]
) -> None:
    """Both nested commands keep the approved scratch output path."""
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.chdir(source)
    scratch = tmp_path / "scratch"
    environment = build_host_verification_env(
        home=scratch / "home",
        temporary=scratch / "tmp",
        cache=scratch / "cache",
        runtime_environment=tmp_path / "runtime",
        executable=Path(sys.executable),
    )
    monkeypatch.setattr(os, "environ", environment)
    result = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
    with patch.object(host_coverage, "run_subprocess", return_value=result) as run:
        assert host_coverage._run(args) == 0
    assert run.call_args.kwargs["env"] == {**environment, "PYTHONPATH": str(source.resolve())}


@pytest.mark.parametrize("coverage_file", [None, "relative", "source", "outside"])
def test_coverage_target_cannot_resolve_below_source_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    coverage_file: str | None,
) -> None:
    """Invalid output paths stop before a child starts and expose no values."""
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.chdir(source)
    scratch = tmp_path / "scratch"
    environment = build_host_verification_env(
        home=scratch / "home",
        temporary=scratch / "tmp",
        cache=scratch / "cache",
        runtime_environment=tmp_path / "runtime",
        executable=Path(sys.executable),
    )
    if coverage_file is None:
        del environment["COVERAGE_FILE"]
    else:
        environment["COVERAGE_FILE"] = {
            "relative": "private-relative-value",
            "source": str(source / ".coverage"),
            "outside": str(tmp_path / "private-output"),
        }[coverage_file]
    monkeypatch.setattr(os, "environ", environment)
    with patch.object(host_coverage, "run_subprocess") as run:
        assert host_coverage._run(host_coverage._UNIT_COVERAGE_ARGS) == 2
    run.assert_not_called()
    assert capsys.readouterr() == ("", "Invalid host verification environment.\n")
