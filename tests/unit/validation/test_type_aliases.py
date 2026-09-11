"""Tests for hephaestus.validation.type_aliases."""

import json
import os
import stat
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

    @pytest.mark.parametrize(
        ("windows_style", "expected_exit_code"),
        [(False, 0), (True, 1)],
        ids=("posix", "windows"),
    )
    def test_recursive_suffix_uses_platform_case_rules(
        self,
        tmp_path: Path,
        windows_style: bool,
        expected_exit_code: int,
    ) -> None:
        """Use the platform-normalized case when recursive selection checks a suffix."""
        source = tmp_path / "source"
        source.mkdir()
        candidate = source / "BAD.PY"
        candidate.write_text("Result = DomainResult\n", encoding="utf-8")

        def controlled_normcase(value: str) -> str:
            return value.lower() if windows_style else value

        with patch("os.path.normcase", side_effect=controlled_normcase):
            exit_code, errors = check_files([source])

        assert exit_code == expected_exit_code
        assert bool(errors) == bool(expected_exit_code)
        if errors:
            assert str(candidate) in errors[0]
            assert "DomainResult" in errors[0]
            assert "Could not read" not in errors[0]

    def test_accepts_file_paths(self, tmp_path: Path) -> None:
        """Individual file paths work."""
        py_file = tmp_path / "single.py"
        py_file.write_text("Result = DomainResult\n")
        exit_code, errors = check_files([py_file])
        assert exit_code == 1
        assert len(errors) == 1

    def test_reports_nested_search_error_and_continues_inputs(self, tmp_path: Path) -> None:
        """Report an inaccessible subtree and scan a later input."""
        source = tmp_path / "source"
        locked = source / "locked"
        locked.mkdir(parents=True)
        (locked / "hidden.py").write_text("x = 1\n", encoding="utf-8")
        sibling = source / "sibling.py"
        sibling.write_text("Runner = TaskRunner\n", encoding="utf-8")
        later = tmp_path / "later.py"
        later.write_text("Result = DomainResult\n", encoding="utf-8")
        real_scandir = os.scandir

        def controlled_scandir(path: Path) -> Iterator[os.DirEntry[str]]:
            if Path(path) == locked:
                raise PermissionError("search denied")
            return real_scandir(path)

        with patch("os.scandir", side_effect=controlled_scandir):
            exit_code, errors = check_files([source, later])

        assert exit_code == 1
        assert any(str(locked) in error and "search denied" in error for error in errors)
        assert any(str(sibling) in error and "TaskRunner" in error for error in errors)
        assert any(str(later) in error and "DomainResult" in error for error in errors)

    def test_python_suffix_directory_is_not_scanned_as_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Scan children but do not read a directory whose name ends in .py."""
        package = tmp_path / "package.py"
        package.mkdir()
        child = package / "child.py"
        child.write_text("Result = DomainResult\n", encoding="utf-8")

        exit_code, errors = check_files([tmp_path])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(child) in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]
        assert capsys.readouterr().err == ""

    def test_reports_entry_classification_error(self, tmp_path: Path) -> None:
        """Report an entry that cannot be classified."""
        source = tmp_path / "source"
        source.mkdir()
        broken = source / "broken.py"
        entry = MagicMock()
        entry.path = str(broken)
        entry.is_junction.return_value = False
        entry.stat.side_effect = OSError("classification failed")
        entries = MagicMock()
        entries.__enter__.return_value = iter([entry])

        with patch("os.scandir", return_value=entries):
            exit_code, errors = check_files([source])

        assert exit_code == 1
        assert errors == [f"Could not read {broken}: classification failed"]

    def test_directory_symlink_is_not_followed(self, tmp_path: Path) -> None:
        """Do not follow a directory link during a recursive scan."""
        source = tmp_path / "source"
        target = tmp_path / "target"
        source.mkdir()
        target.mkdir()
        (target / "hidden.py").write_text("Result = DomainResult\n", encoding="utf-8")
        (source / "linked").symlink_to(target, target_is_directory=True)

        exit_code, errors = check_files([source])

        assert exit_code == 0
        assert errors == []

    def test_explicit_directory_symlink_is_scanned_as_root(self, tmp_path: Path) -> None:
        """Scan a directory link that an explicit input selects."""
        target = tmp_path / "target"
        outside = tmp_path / "outside"
        target.mkdir()
        outside.mkdir()
        (target / "hidden.py").write_text("Result = DomainResult\n", encoding="utf-8")
        (outside / "outside.py").write_text("Runner = TaskRunner\n", encoding="utf-8")
        (target / "nested").symlink_to(outside, target_is_directory=True)
        linked = tmp_path / "src-link"
        linked.symlink_to(target, target_is_directory=True)

        exit_code, errors = check_files([linked])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(linked / "hidden.py") in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]

    def test_explicit_file_symlink_is_scanned(self, tmp_path: Path) -> None:
        """Scan a regular Python file that an explicit link selects."""
        target = tmp_path / "target.py"
        target.write_text("Result = DomainResult\n", encoding="utf-8")
        linked = tmp_path / "linked.py"
        linked.symlink_to(target)

        exit_code, errors = check_files([linked])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(linked) in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]

    def test_broken_explicit_python_symlink_is_read_error(self, tmp_path: Path) -> None:
        """Report a broken Python file link as an incomplete scan."""
        linked = tmp_path / "linked.py"
        linked.symlink_to(tmp_path / "missing.py")

        exit_code, errors = check_files([linked])

        assert exit_code == 1
        assert len(errors) == 1
        assert f"Could not read {linked}:" in errors[0]

    def test_recursive_file_symlink_is_scanned(self, tmp_path: Path) -> None:
        """Scan a regular file link that recursive discovery finds."""
        source = tmp_path / "source"
        source.mkdir()
        target = tmp_path / "target.py"
        target.write_text("Result = DomainResult\n", encoding="utf-8")
        (source / "linked.py").symlink_to(target)

        exit_code, errors = check_files([source])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(source / "linked.py") in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]

    def test_broken_recursive_python_symlink_is_read_error(self, tmp_path: Path) -> None:
        """Report a broken Python file link found during recursion."""
        source = tmp_path / "source"
        source.mkdir()
        linked = source / "linked.py"
        linked.symlink_to(tmp_path / "missing.py")

        exit_code, errors = check_files([source])

        assert exit_code == 1
        assert len(errors) == 1
        assert f"Could not read {linked}:" in errors[0]

    def test_reports_recursive_symlink_target_error(self, tmp_path: Path) -> None:
        """Report a file link target that cannot be classified."""
        source = tmp_path / "source"
        source.mkdir()
        linked = source / "linked.py"
        entry = MagicMock()
        entry.path = str(linked)
        entry.name = linked.name
        entry.is_junction.return_value = False
        link_status = MagicMock()
        link_status.st_mode = stat.S_IFLNK
        entry.stat.side_effect = [link_status, PermissionError("target denied")]
        entries = MagicMock()
        entries.__enter__.return_value = iter([entry])

        with patch("os.scandir", return_value=entries):
            exit_code, errors = check_files([source])

        assert exit_code == 1
        assert errors == [f"Could not read {linked}: target denied"]

    def test_explicit_junction_is_scanned_as_root(self, tmp_path: Path) -> None:
        """Scan an explicit junction root on platforms that support it."""
        root = tmp_path / "junction"
        root.mkdir()
        child = root / "child.py"
        child.write_text("Result = DomainResult\n", encoding="utf-8")

        def controlled_is_junction(path: Path) -> bool:
            return path == root

        with patch.object(Path, "is_junction", controlled_is_junction):
            exit_code, errors = check_files([root])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(child) in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]

    def test_nested_junction_is_scanned(self, tmp_path: Path) -> None:
        """Scan a junction found during recursive discovery."""
        source = tmp_path / "source"
        source.mkdir()
        junction = source / "junction"
        junction.mkdir()
        child = junction / "child.py"
        child.write_text("Result = DomainResult\n", encoding="utf-8")
        entry = MagicMock()
        entry.path = str(junction)
        entry.is_junction.return_value = True
        entries = MagicMock()
        entries.__enter__.return_value = iter([entry])
        real_scandir = os.scandir

        def controlled_scandir(path: Path) -> Iterator[os.DirEntry[str]]:
            if Path(path) == source:
                return entries
            return real_scandir(path)

        with patch("os.scandir", side_effect=controlled_scandir):
            exit_code, errors = check_files([source])

        assert exit_code == 1
        assert len(errors) == 1
        assert str(child) in errors[0]
        assert "DomainResult" in errors[0]
        assert "Could not read" not in errors[0]
        entry.stat.assert_not_called()


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
    real_stat = Path.stat

    def controlled_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == locked:
            raise error
        return real_stat(path, follow_symlinks=follow_symlinks)

    with patch.object(Path, "stat", controlled_stat):
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


@pytest.mark.parametrize("kind", ["missing_path", "dangling_directory_link"])
def test_missing_explicit_input_text_and_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: str,
) -> None:
    """Report a missing explicit input and continue with a later file."""
    missing = tmp_path / "missing-src"
    if kind == "dangling_directory_link":
        missing = tmp_path / "directory-link"
        missing.symlink_to(tmp_path / "missing-directory", target_is_directory=True)
    later = tmp_path / "later.py"
    later.write_text("Result = DomainResult\n", encoding="utf-8")
    paths = [missing, later]

    monkeypatch.setattr("sys.argv", ["check-type-aliases", *map(str, paths)])
    assert main() == 1
    text_output = capsys.readouterr()
    monkeypatch.setattr("sys.argv", ["check-type-aliases", "--json", *map(str, paths)])
    assert main() == 1
    json_output = capsys.readouterr()
    payload = json.loads(json_output.out)

    assert text_output.out == json_output.err == ""
    assert f"Could not read {missing}:" in text_output.err
    assert str(later) in text_output.err
    assert "DomainResult" in text_output.err
    assert payload["passed"] is False
    assert payload["scan_complete"] is False
    assert payload["exit_code"] == 1
    assert payload["read_error_count"] == 1
    assert payload["violation_count"] == 1
    assert f"Could not read {missing}:" in payload["read_errors"][0]
    assert str(later) in payload["violations"][0]
    assert "DomainResult" in payload["violations"][0]


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
    first_findings = [item for item in errors if str(first) in item and "DomainResult" in item]
    first_read_errors = [item for item in errors if str(first) in item and str(error) in item]
    later_findings = [item for item in errors if str(later) in item and "TaskRunner" in item]
    assert len(first_findings) == 1
    assert "Could not read" not in first_findings[0]
    assert len(first_read_errors) == 1
    assert first_read_errors[0].startswith(f"Could not read {first}:")
    assert len(later_findings) == 1
    assert "Could not read" not in later_findings[0]


def test_earlier_finding_survives_later_read_error(tmp_path: Path) -> None:
    """Keep an earlier finding when a later file cannot be read."""
    first = tmp_path / "first.py"
    first.write_text("Result = DomainResult\n", encoding="utf-8")
    later = tmp_path / "later.py"
    real_open = open

    def controlled_open(file: Path, *, encoding: str) -> TextIO:
        if file == later:
            raise PermissionError("read denied")
        return real_open(file, encoding=encoding)

    with patch("builtins.open", side_effect=controlled_open):
        exit_code, errors = check_files([first, later])

    assert exit_code == 1
    assert len(errors) == 2
    assert str(first) in errors[0]
    assert "DomainResult" in errors[0]
    assert str(later) in errors[1]
    assert "read denied" in errors[1]


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
