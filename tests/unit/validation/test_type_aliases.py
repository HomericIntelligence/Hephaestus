"""Tests for hephaestus.validation.type_aliases."""

import json
import subprocess
import sys
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.validation.type_aliases import (
    _update_string_state,
    check_files,
    detect_shadowing,
    format_error,
    is_shadowing_pattern,
    main,
)


class TestIsShadowingPattern:
    """Tests for is_shadowing_pattern()."""

    def test_suffix_shadowing_detected(self) -> None:
        """Generic name that is a suffix of the target is flagged."""
        assert is_shadowing_pattern("Result", "DomainResult") is True

    def test_multi_word_suffix_shadowing(self) -> None:
        """Multi-word alias that is a suffix of the target is flagged."""
        assert is_shadowing_pattern("RunResult", "ExecutorRunResult") is True

    def test_equal_names_not_flagged(self) -> None:
        """Identical names are not shadowing."""
        assert is_shadowing_pattern("Result", "Result") is False

    def test_non_suffix_not_flagged(self) -> None:
        """Alias that is not a suffix of target is not flagged."""
        assert is_shadowing_pattern("AggregatedStats", "Statistics") is False

    def test_case_insensitive(self) -> None:
        """Comparison is case-insensitive."""
        assert is_shadowing_pattern("result", "DomainResult") is True

    def test_unrelated_names(self) -> None:
        """Completely unrelated names are not flagged."""
        assert is_shadowing_pattern("Foo", "Bar") is False


class TestDetectShadowing:
    """Tests for detect_shadowing()."""

    def test_detects_shadowing_in_file(self, tmp_path: Path) -> None:
        """Detects a simple shadowing pattern in a Python file."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 1
        assert violations[0][2] == "Result"
        assert violations[0][3] == "DomainResult"

    def test_ignores_non_shadowing(self, tmp_path: Path) -> None:
        """Does not flag non-shadowing assignments."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Stats = AggregatedStatistics\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_suppressed_lines(self, tmp_path: Path) -> None:
        """Lines with # type: ignore[shadowing] are skipped."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult  # type: ignore[shadowing]\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_noqa_lines(self, tmp_path: Path) -> None:
        """Lines with # noqa: shadowing are skipped."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult  # noqa: shadowing\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_docstrings(self, tmp_path: Path) -> None:
        """Content inside triple-quoted strings is ignored."""
        py_file = tmp_path / "example.py"
        py_file.write_text('"""\nResult = DomainResult\n"""\nx = 1\n')
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_handles_missing_file(self, tmp_path: Path) -> None:
        """Missing files return empty violations."""
        py_file = tmp_path / "nonexistent.py"
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_skips_lowercase_assignments(self, tmp_path: Path) -> None:
        """Only PascalCase identifiers are checked."""
        py_file = tmp_path / "example.py"
        py_file.write_text("result = domain_result\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 0

    def test_multiple_violations(self, tmp_path: Path) -> None:
        """Multiple violations in one file are all detected."""
        py_file = tmp_path / "example.py"
        py_file.write_text("Result = DomainResult\nRunner = TaskRunner\n")
        violations = detect_shadowing(py_file)
        assert len(violations) == 2


class TestFormatError:
    """Tests for format_error()."""

    def test_includes_all_info(self) -> None:
        """Error message includes file, line, and suggestion."""
        msg = format_error(Path("foo.py"), 10, "Result = DomainResult", "Result", "DomainResult")
        assert "foo.py:10" in msg
        assert "Result = DomainResult" in msg
        assert "DomainResult" in msg
        assert "type: ignore[shadowing]" in msg


class TestCheckFiles:
    """Tests for check_files()."""

    def test_clean_directory(self, tmp_path: Path) -> None:
        """Directory with no violations returns exit code 0."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        exit_code, errors = check_files([tmp_path])
        assert exit_code == 0
        assert errors == []

    def test_directory_with_violations(self, tmp_path: Path) -> None:
        """Directory with violations returns exit code 1."""
        py_file = tmp_path / "bad.py"
        py_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([tmp_path])
        assert exit_code == 1
        assert len(errors) == 1

    def test_skips_non_python_files(self, tmp_path: Path) -> None:
        """Non-Python files are skipped."""
        txt_file = tmp_path / "notes.txt"
        txt_file.write_text("Result = DomainResult\n")
        exit_code, _errors = check_files([tmp_path])
        assert exit_code == 0

    def test_accepts_file_paths(self, tmp_path: Path) -> None:
        """Individual file paths work."""
        py_file = tmp_path / "single.py"
        py_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([py_file])
        assert exit_code == 1
        assert len(errors) == 1


class TestUpdateStringState:
    """Tests for _update_string_state()."""

    def test_enter_double_quote_string(self) -> None:
        """Entering a triple double-quoted string."""
        in_str, delim = _update_string_state('"""docstring"""', False, None)
        assert in_str is True
        assert delim == '"""'

    def test_exit_double_quote_string(self) -> None:
        """Exiting a triple double-quoted string."""
        in_str, delim = _update_string_state('"""', True, '"""')
        assert in_str is False
        assert delim is None

    def test_enter_single_quote_string(self) -> None:
        """Entering a triple single-quoted string."""
        in_str, delim = _update_string_state("'''docstring'''", False, None)
        assert in_str is True
        assert delim == "'''"

    def test_no_change_for_normal_line(self) -> None:
        """Normal lines don't change string state."""
        in_str, _delim = _update_string_state("x = 1", False, None)
        assert in_str is False


class TestMain:
    """Tests for main() CLI entry point."""

    def test_clean_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Clean code exits 0."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        monkeypatch.setattr("sys.argv", ["check-type-aliases", str(tmp_path)])
        assert main() == 0

    def test_violations_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Code with violations exits 1."""
        py_file = tmp_path / "bad.py"
        py_file.write_text("Result = DomainResult\n")
        monkeypatch.setattr("sys.argv", ["check-type-aliases", str(tmp_path)])
        assert main() == 1

    def test_verbose_flag(self, tmp_path: Path, monkeypatch) -> None:
        """Verbose flag is accepted."""
        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        monkeypatch.setattr("sys.argv", ["check-type-aliases", "--verbose", str(tmp_path)])
        assert main() == 0

    def test_clean_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--json emits a passing report for clean code."""
        import json

        py_file = tmp_path / "clean.py"
        py_file.write_text("x = 1\n")
        monkeypatch.setattr("sys.argv", ["check-type-aliases", "--json", str(tmp_path)])
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["violation_count"] == 0

    def test_violations_json(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--json emits a failing report listing violations."""
        import json

        py_file = tmp_path / "bad.py"
        py_file.write_text("Result = DomainResult\n")
        monkeypatch.setattr("sys.argv", ["check-type-aliases", "--json", str(tmp_path)])
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False
        assert payload["violation_count"] >= 1


@pytest.mark.parametrize(
    "kind", ["clean", "violations", "missing_file", "permission_error", "invalid_encoding"]
)
@pytest.mark.parametrize("verbose", [False, True])
def test_read_diagnostics_json_consistency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    verbose: bool,
) -> None:
    """Keep read errors separate from violations and return the reported status."""
    path = tmp_path / "input.py"
    if kind != "missing_file":
        path.write_bytes(
            b"\xff"
            if kind == "invalid_encoding"
            else b"Result = DomainResult\n"
            if kind == "violations"
            else b"x = 1\n"
        )
    with (
        patch("builtins.open", side_effect=PermissionError("read denied"))
        if kind == "permission_error"
        else nullcontext()
    ):
        code, errors = check_files([path])
        assert code == (0 if kind == "clean" else 1)
        assert bool(errors) == (kind != "clean")
        monkeypatch.setattr(
            "sys.argv",
            ["check-type-aliases", "--json", *(["--verbose"] if verbose else []), str(path)],
        )
        result = main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    failed_read = kind not in ("clean", "violations")
    assert captured.err == ""
    assert payload["exit_code"] == result == code
    assert payload["passed"] == (code == 0)
    assert payload["scan_complete"] == (not failed_read)
    assert payload["read_error_count"] == int(failed_read)
    assert payload["violation_count"] == int(kind == "violations")
    if failed_read:
        assert str(path) in payload["read_errors"][0]
        assert payload["read_errors"] == errors
        cause = {
            "missing_file": "No such file or directory",
            "permission_error": "read denied",
            "invalid_encoding": "utf-8",
        }[kind]
        assert cause in payload["read_errors"][0]


@pytest.mark.parametrize("error", [PermissionError("search denied"), OSError("stat failed")])
@pytest.mark.parametrize("with_violation", [False, True])
def test_selection_error_diagnostics_consistency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: OSError,
    with_violation: bool,
) -> None:
    """Report selection errors in both formats and scan later inputs."""
    locked = tmp_path / "locked" / "input.py"
    locked.parent.mkdir()
    locked.write_text("x = 1\n", encoding="utf-8")
    later = tmp_path / "later.py"
    later.write_text("Result = DomainResult\n" if with_violation else "x = 1\n", encoding="utf-8")
    paths = [locked, later]
    real_is_dir = Path.is_dir

    def controlled_is_dir(path: Path) -> bool:
        if path == locked:
            raise error
        return real_is_dir(path)

    with patch.object(Path, "is_dir", controlled_is_dir):
        code, errors = check_files(paths)
        monkeypatch.setattr("sys.argv", ["check-type-aliases", *map(str, paths)])
        text_code = main()
        text_output = capsys.readouterr()
        monkeypatch.setattr(
            "sys.argv", ["check-type-aliases", "--json", "--verbose", *map(str, paths)]
        )
        json_code = main()
    json_output = capsys.readouterr()
    payload = json.loads(json_output.out)
    assert code == text_code == json_code == payload["exit_code"] == 1
    assert text_output.out == json_output.err == ""
    assert payload["paths"] == list(map(str, paths))
    assert payload["passed"] is False
    assert payload["scan_complete"] is False
    assert payload["read_error_count"] == len(payload["read_errors"]) == 1
    assert str(locked) in payload["read_errors"][0]
    assert str(error) in payload["read_errors"][0]
    assert payload["violation_count"] == len(payload["violations"]) == int(with_violation)
    assert errors == payload["violations"] + payload["read_errors"]
    assert all(item in text_output.err for item in errors)
    assert "Scan incomplete: 1 read error(s)" in text_output.err
    if with_violation:
        assert str(later) in payload["violations"][0]
        assert "DomainResult" in payload["violations"][0]
        assert "Found 1 type alias shadowing violation(s)" in text_output.err
    else:
        assert "violation(s)" not in text_output.err


@pytest.mark.parametrize(
    "error",
    [PermissionError("read denied"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid byte")],
)
def test_partial_read_mixed_inputs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
) -> None:
    """Preserve partial findings and scan later files after a read error."""
    first = tmp_path / "first.py"
    later = tmp_path / "later.py"
    later.write_text("Runner = TaskRunner\n", encoding="utf-8")
    real_open = open

    def read_lines() -> Iterator[str]:
        yield "Result = DomainResult\n"
        raise error

    with patch("builtins.open") as mocked:
        stream = MagicMock()
        stream.__enter__.return_value = read_lines()
        mocked.return_value = stream
        assert detect_shadowing(first) == [(1, "Result = DomainResult", "Result", "DomainResult")]
    assert f"Warning: Could not read {first}: {error}" in capsys.readouterr().err
    with real_open(later, encoding="utf-8") as later_stream, patch("builtins.open") as mocked:
        stream = MagicMock()
        stream.__enter__.return_value = read_lines()
        mocked.side_effect = [stream, later_stream]
        code, errors = check_files([first, later])
        assert mocked.call_count == 2
    assert code == 1
    assert len(errors) == 3
    assert any(str(first) in item and str(error) in item for item in errors)
    assert any(str(later) in item and "TaskRunner" in item for item in errors)


@pytest.mark.parametrize("invalid_encoding", [False, True])
@pytest.mark.parametrize("json_mode", [False, True])
def test_subprocess_read_diagnostics(
    tmp_path: Path,
    invalid_encoding: bool,
    json_mode: bool,
) -> None:
    """Report the same read status to the shell and JSON consumers."""
    path = tmp_path / "input.py"
    path.write_bytes(b"\xff" if invalid_encoding else b"x = 1\n")
    result = subprocess.run(
        [
            sys.executable,
            # The package imports this module before runpy executes it.
            "-W",
            "ignore:'hephaestus.validation.type_aliases' found in sys.modules:RuntimeWarning",
            "-m",
            "hephaestus.validation.type_aliases",
            str(path),
            *(["--json", "--verbose"] if json_mode else []),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == int(invalid_encoding)
    if json_mode:
        payload = json.loads(result.stdout)
        assert payload["passed"] == (result.returncode == 0)
        assert payload["exit_code"] == result.returncode
        assert payload["scan_complete"] == (not invalid_encoding)
        assert result.stderr == ""
    elif invalid_encoding:
        assert str(path) in result.stderr
        assert "utf-8" in result.stderr
        assert "incomplete" in result.stderr.lower()
        assert "violation(s)" not in result.stderr


@pytest.mark.parametrize("kind", ["missing_file", "permission_error", "invalid_encoding"])
def test_helper_read_diagnostics_compatibility(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """Keep the public helper's list result and warning on failed reads."""
    path = tmp_path / "input.py"
    if kind == "invalid_encoding":
        path.write_bytes(b"\xff")
    with (
        patch("builtins.open", side_effect=PermissionError("read denied"))
        if kind == "permission_error"
        else nullcontext()
    ):
        assert detect_shadowing(path) == []
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"Warning: Could not read {path}:" in captured.err


def test_mixed_inputs_json_consistency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Report read failures and later findings in one JSON result."""
    unreadable = tmp_path / "invalid.py"
    unreadable.write_bytes(b"\xff")
    clean = tmp_path / "clean.py"
    clean.write_text("x = 1\n", encoding="utf-8")
    violation = tmp_path / "violation.py"
    violation.write_text("Result = DomainResult\n", encoding="utf-8")
    paths = [unreadable, clean, violation]
    monkeypatch.setattr("sys.argv", ["check-type-aliases", "--json", *map(str, paths)])
    assert main() == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert captured.err == ""
    assert payload["paths"] == list(map(str, paths))
    assert payload["exit_code"] == 1
    assert payload["passed"] is False
    assert payload["scan_complete"] is False
    assert payload["read_error_count"] == 1
    assert payload["violation_count"] == 1
    assert str(unreadable) in payload["read_errors"][0]
    assert str(violation) in payload["violations"][0]


@pytest.mark.parametrize("kind", ["missing_file", "permission_error", "invalid_encoding"])
@pytest.mark.parametrize("with_violation", [False, True])
def test_text_read_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    with_violation: bool,
) -> None:
    """Report the read cause and count findings separately in text output."""
    path = tmp_path / "input.py"
    if kind == "invalid_encoding":
        path.write_bytes(b"\xff")
    paths = [path]
    later = tmp_path / "later.py"
    if with_violation:
        later.write_text("Result = DomainResult\n", encoding="utf-8")
        paths.append(later)
    real_open = open

    def controlled_open(file: Path, *, encoding: str) -> TextIO:
        if file == path:
            raise PermissionError("read denied")
        return real_open(file, encoding=encoding)

    monkeypatch.setattr("sys.argv", ["check-type-aliases", *map(str, paths)])
    with (
        patch("builtins.open", side_effect=controlled_open)
        if kind == "permission_error"
        else nullcontext()
    ):
        assert main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert str(path) in captured.err
    cause = {
        "missing_file": "No such file or directory",
        "permission_error": "read denied",
        "invalid_encoding": "utf-8",
    }[kind]
    assert cause in captured.err
    assert "Scan incomplete: 1 read error(s)" in captured.err
    if with_violation:
        assert str(later) in captured.err
        assert "Found 1 type alias shadowing violation(s)" in captured.err
    else:
        assert "violation(s)" not in captured.err
