"""Tests for the boundary error contract.

These are core-library behaviour tests: `hephaestus/errors.py` is a library
module that every console script depends on.
"""

from __future__ import annotations

import io
import subprocess

import pytest

from hephaestus.errors import (
    BoundaryError,
    ConfigurationError,
    ExitCode,
    ExternalToolError,
    InvalidInputError,
    NotFoundError,
    TimeoutExpiredError,
    boundary_main,
    format_error,
)


def test_same_cause_renders_the_same_text_twice() -> None:
    """A repeated failure produces byte-identical output."""
    first = format_error(InvalidInputError("branch is required", recovery="Pass --branch."))
    second = format_error(InvalidInputError("branch is required", recovery="Pass --branch."))
    assert first.text == second.text
    assert first.exit_code == second.exit_code


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidInputError("bad"), ExitCode.INVALID_INPUT),
        (NotFoundError("gone"), ExitCode.NOT_FOUND),
        (ConfigurationError("unset"), ExitCode.CONFIGURATION),
        (ExternalToolError("git failed"), ExitCode.EXTERNAL_TOOL),
        (TimeoutExpiredError("too slow"), ExitCode.TIMEOUT),
        (ValueError("plain"), ExitCode.ERROR),
        (TimeoutError("socket"), ExitCode.TIMEOUT),
        (FileNotFoundError("no path"), ExitCode.NOT_FOUND),
        (KeyError("field"), ExitCode.NOT_FOUND),
    ],
)
def test_each_error_class_maps_to_its_exit_code(error: BaseException, expected: ExitCode) -> None:
    """Every error class reports its documented exit code."""
    assert format_error(error).exit_code is expected


def test_recovery_text_is_included_when_supplied() -> None:
    """A recovery instruction reaches the operator."""
    report = format_error(NotFoundError("worktree gone", recovery="Run cleanup again."))
    assert "Recovery: Run cleanup again." in report.text


def test_recovery_line_is_absent_when_not_supplied() -> None:
    """An error without recovery text emits no empty recovery line."""
    assert "Recovery:" not in format_error(NotFoundError("worktree gone")).text


def test_boundary_main_returns_zero_for_success() -> None:
    """An entry point that succeeds reports OK."""
    assert boundary_main(lambda: None, stream=io.StringIO()) == 0


def test_boundary_main_returns_an_explicit_int_result() -> None:
    """An entry point may choose its own success code."""
    assert boundary_main(lambda: 7, stream=io.StringIO()) == 7


def test_boundary_main_writes_the_report_and_returns_the_code() -> None:
    """A failing entry point writes one report and returns its exit code."""
    stream = io.StringIO()

    def boom() -> None:
        raise NotFoundError("no such branch", recovery="Fetch the branch.")

    code = boundary_main(boom, stream=stream)
    assert code == int(ExitCode.NOT_FOUND)
    assert "NotFoundError: no such branch" in stream.getvalue()
    assert "Recovery: Fetch the branch." in stream.getvalue()


def test_boundary_main_reports_an_unexpected_exception_without_reraising() -> None:
    """An unclassified failure still becomes a report, not a traceback."""
    stream = io.StringIO()

    def boom() -> None:
        raise RuntimeError("unclassified")

    assert boundary_main(boom, stream=stream) == int(ExitCode.ERROR)
    assert "RuntimeError: unclassified" in stream.getvalue()


def test_boundary_main_reports_an_interrupt_as_130() -> None:
    """Ctrl+C exits 130 rather than raising."""
    stream = io.StringIO()

    def boom() -> None:
        raise KeyboardInterrupt

    assert boundary_main(boom, stream=stream) == int(ExitCode.INTERRUPTED)
    assert stream.getvalue() == "Interrupted.\n"


def test_boundary_main_passes_through_system_exit_code() -> None:
    """Argparse exit 2 stays 2 instead of becoming a generic failure."""
    stream = io.StringIO()

    def boom() -> None:
        raise SystemExit(2)

    assert boundary_main(boom, stream=stream) == 2
    assert stream.getvalue() == ""


def test_boundary_main_treats_a_bare_system_exit_as_success() -> None:
    """``sys.exit()`` with no code means success."""
    stream = io.StringIO()

    def boom() -> None:
        raise SystemExit

    assert boundary_main(boom, stream=stream) == int(ExitCode.OK)


def test_boundary_main_reports_a_string_system_exit() -> None:
    """A non-integer exit code becomes a reported failure."""
    stream = io.StringIO()

    def boom() -> None:
        raise SystemExit("bad usage")

    assert boundary_main(boom, stream=stream) == int(ExitCode.ERROR)
    assert "bad usage" in stream.getvalue()


def test_boundary_main_forwards_arguments() -> None:
    """Positional and keyword arguments reach the entry point."""
    seen: dict[str, object] = {}

    def entry(value: str, *, flag: bool) -> None:
        seen["value"] = value
        seen["flag"] = flag

    assert boundary_main(entry, "x", flag=True, stream=io.StringIO()) == 0
    assert seen == {"value": "x", "flag": True}


def test_boundary_error_accepts_a_per_instance_exit_code_override() -> None:
    """A specific cause can override the class default."""
    error = BoundaryError("odd", exit_code=ExitCode.EXTERNAL_TOOL)
    assert error.exit_code is ExitCode.EXTERNAL_TOOL


def test_console_script_wrapper_routes_through_boundary_main(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An installed console-script entry reports through the shared boundary.

    A wrapper without the boundary would re-raise; the wired entry returns the
    boundary exit code and writes the boundary report instead.
    """
    import hephaestus.validation.cli_tier_docs as cli_tier_docs_cli

    def fail(_argv: list[str] | None = None) -> int:
        raise ConfigurationError("required python version file is missing")

    monkeypatch.setattr(cli_tier_docs_cli, "_run", fail)

    assert cli_tier_docs_cli.main([]) == int(ExitCode.CONFIGURATION)
    captured = capsys.readouterr()
    assert captured.err == "ConfigurationError: required python version file is missing\n"


def test_importing_the_library_does_not_load_automation() -> None:
    """`hephaestus.errors` must not pull the product layer in.

    This is a real import-boundary behaviour check, not a config assertion.
    """
    probe = (
        "import sys, hephaestus.errors; "
        "print(','.join(m for m in sys.modules if m.startswith('hephaestus.automation')))"
    )
    result = subprocess.run(
        ["python", "-c", probe],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == ""
