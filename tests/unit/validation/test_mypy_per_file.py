"""Tests for hephaestus.validation.mypy_per_file."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.validation.mypy_per_file import (
    check_mypy_per_file,
    main,
    run_mypy_per_file,
    split_flags_and_files,
)


class TestSplitFlagsAndFiles:
    """Tests for split_flags_and_files()."""

    def test_no_args(self) -> None:
        flags, files = split_flags_and_files([])
        assert flags == []
        assert files == []

    def test_only_files(self) -> None:
        flags, files = split_flags_and_files(["a.py", "b.py"])
        assert flags == []
        assert files == ["a.py", "b.py"]

    def test_only_flags(self) -> None:
        flags, files = split_flags_and_files(["--strict", "--ignore-missing-imports"])
        assert flags == ["--strict", "--ignore-missing-imports"]
        assert files == []

    def test_mixed(self) -> None:
        flags, files = split_flags_and_files(
            ["--ignore-missing-imports", "a.py", "--strict", "b.py"]
        )
        assert "--ignore-missing-imports" in flags
        assert "--strict" in flags
        assert "a.py" in files
        assert "b.py" in files

    def test_flag_with_value(self) -> None:
        flags, files = split_flags_and_files(["--python-version", "3.10", "myfile.py"])
        assert "--python-version" in flags
        assert "3.10" in flags
        assert "myfile.py" in files

    def test_config_file_flag(self) -> None:
        flags, files = split_flags_and_files(["--config-file", "mypy.ini", "code.py"])
        assert "--config-file" in flags
        assert "mypy.ini" in flags
        assert "code.py" in files

    def test_short_flags(self) -> None:
        flags, files = split_flags_and_files(["-v", "file.py"])
        assert "-v" in flags
        assert "file.py" in files


class TestRunMypyPerFile:
    """Tests for run_mypy_per_file()."""

    def test_no_files_returns_zero(self, capsys: pytest.CaptureFixture) -> None:
        result = run_mypy_per_file([], flags=[])
        assert result == 0
        captured = capsys.readouterr()
        assert "no files" in captured.err

    def test_all_pass_returns_zero(self, tmp_path: Path) -> None:
        f = tmp_path / "clean.py"
        f.write_text("x: int = 1\n")
        result = run_mypy_per_file([str(f)], flags=["--ignore-missing-imports"])
        assert result == 0

    def test_type_error_returns_nonzero(self, tmp_path: Path) -> None:
        f = tmp_path / "bad.py"
        # Assign wrong type to a typed variable
        f.write_text('x: int = "not an int"\n')
        result = run_mypy_per_file([str(f)], flags=["--ignore-missing-imports"])
        assert result != 0

    def test_aggregates_multiple_files(self, tmp_path: Path) -> None:
        good = tmp_path / "good.py"
        good.write_text("x: int = 1\n")
        bad = tmp_path / "bad.py"
        bad.write_text('y: int = "wrong"\n')
        result = run_mypy_per_file([str(good), str(bad)], flags=["--ignore-missing-imports"])
        assert result != 0

    def test_custom_python_executable(self, tmp_path: Path) -> None:
        f = tmp_path / "clean.py"
        f.write_text("x = 1\n")
        result = run_mypy_per_file(
            [str(f)],
            flags=["--ignore-missing-imports"],
            python_executable=sys.executable,
        )
        assert result == 0

    def test_subprocess_called_per_file(self, tmp_path: Path) -> None:
        files = [str(tmp_path / f"f{i}.py") for i in range(3)]
        for f in files:
            Path(f).write_text("x = 1\n")

        call_count = 0
        original_run = __import__("subprocess").run

        def counting_run(cmd, **kwargs):
            nonlocal call_count
            if "mypy" in cmd:
                call_count += 1
            return original_run(cmd, **kwargs)

        with patch("subprocess.run", side_effect=counting_run):
            run_mypy_per_file(files, flags=["--ignore-missing-imports"])

        assert call_count == 3


class TestCheckMypyPerFile:
    """Tests for check_mypy_per_file()."""

    def test_path_objects_accepted(self, tmp_path: Path) -> None:
        f = tmp_path / "clean.py"
        f.write_text("x: int = 1\n")
        result = check_mypy_per_file([f], flags=["--ignore-missing-imports"])
        assert result == 0

    def test_string_paths_accepted(self, tmp_path: Path) -> None:
        f = tmp_path / "clean.py"
        f.write_text("x: int = 1\n")
        result = check_mypy_per_file([str(f)], flags=["--ignore-missing-imports"])
        assert result == 0


class TestMain:
    """Tests for main() CLI entry point."""

    def test_no_files_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-mypy-each-file"])
        result = main()
        assert result == 0

    def test_clean_file_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "clean.py"
        f.write_text("x: int = 1\n")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-mypy-each-file", "--ignore-missing-imports", str(f)],
        )
        assert main() == 0

    def test_help_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-mypy-each-file", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0
