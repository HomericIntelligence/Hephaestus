"""Tests for hephaestus.scripts_lib.scaffold_subpackage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.scripts_lib.scaffold_subpackage import main


def _run(args: list[str], tmp_path: Path) -> int:
    """Run main() with --root pointing at tmp_path."""
    return main(["--root", str(tmp_path), *args])


class TestValidNames:
    """Happy-path: valid snake_case name produces expected file tree."""

    def test_creates_package_init(self, tmp_path: Path) -> None:
        rc = _run(["myutils"], tmp_path)
        assert rc == 0
        pkg_init = tmp_path / "hephaestus" / "myutils" / "__init__.py"
        assert pkg_init.exists()
        assert "Myutils" in pkg_init.read_text() or "myutils" in pkg_init.read_text()

    def test_creates_module_stub(self, tmp_path: Path) -> None:
        rc = _run(["myutils"], tmp_path)
        assert rc == 0
        module = tmp_path / "hephaestus" / "myutils" / "myutils.py"
        assert module.exists()

    def test_creates_test_init(self, tmp_path: Path) -> None:
        rc = _run(["myutils"], tmp_path)
        assert rc == 0
        test_init = tmp_path / "tests" / "unit" / "myutils" / "__init__.py"
        assert test_init.exists()

    def test_creates_test_module(self, tmp_path: Path) -> None:
        rc = _run(["myutils"], tmp_path)
        assert rc == 0
        test_module = tmp_path / "tests" / "unit" / "myutils" / "test_myutils.py"
        assert test_module.exists()

    def test_underscore_name(self, tmp_path: Path) -> None:
        rc = _run(["my_utils"], tmp_path)
        assert rc == 0
        assert (tmp_path / "hephaestus" / "my_utils" / "__init__.py").exists()

    def test_prints_next_steps(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(["myutils"], tmp_path)
        out = capsys.readouterr().out
        assert "myutils" in out


class TestInvalidNames:
    """Invalid names must exit non-zero and write nothing."""

    @pytest.mark.parametrize(
        "name",
        [
            "Bad-Name",
            "1abc",
            "",
            "Has Space",
            "CamelCase",
            "has.dot",
        ],
    )
    def test_invalid_name_nonzero_exit(self, name: str, tmp_path: Path) -> None:
        args = [name] if name else []
        rc = main(["--root", str(tmp_path), *args]) if name else 1
        assert rc != 0

    @pytest.mark.parametrize("name", ["Bad-Name", "1abc", "CamelCase"])
    def test_invalid_name_writes_nothing(self, name: str, tmp_path: Path) -> None:
        main(["--root", str(tmp_path), name])
        assert not (tmp_path / "hephaestus").exists()


class TestExistingTargetRefusal:
    """Refuses to overwrite if target directory already exists."""

    def test_refuses_existing_package_dir(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "hephaestus" / "myutils"
        pkg_dir.mkdir(parents=True)
        rc = _run(["myutils"], tmp_path)
        assert rc != 0

    def test_existing_dir_writes_nothing_new(self, tmp_path: Path) -> None:
        pkg_dir = tmp_path / "hephaestus" / "myutils"
        pkg_dir.mkdir(parents=True)
        before = set(tmp_path.rglob("*"))
        _run(["myutils"], tmp_path)
        after = set(tmp_path.rglob("*"))
        assert after == before


class TestDryRun:
    """--dry-run prints planned paths but writes nothing."""

    def test_dry_run_exits_zero(self, tmp_path: Path) -> None:
        rc = _run(["--dry-run", "myutils"], tmp_path)
        assert rc == 0

    def test_dry_run_writes_nothing(self, tmp_path: Path) -> None:
        _run(["--dry-run", "myutils"], tmp_path)
        assert not (tmp_path / "hephaestus").exists()
        assert not (tmp_path / "tests").exists()

    def test_dry_run_prints_paths(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(["--dry-run", "myutils"], tmp_path)
        out = capsys.readouterr().out
        assert "myutils" in out


class TestWithCli:
    """--with-cli generates a scripts/ shim and prints pyproject.toml hint."""

    def test_with_cli_creates_shim(self, tmp_path: Path) -> None:
        rc = _run(["--with-cli", "myutils"], tmp_path)
        assert rc == 0
        shim = tmp_path / "scripts" / "myutils.py"
        assert shim.exists()

    def test_with_cli_shim_contents(self, tmp_path: Path) -> None:
        _run(["--with-cli", "myutils"], tmp_path)
        shim = tmp_path / "scripts" / "myutils.py"
        text = shim.read_text()
        assert "hephaestus.scripts_lib.myutils" in text
        assert "main" in text

    def test_with_cli_prints_project_scripts_hint(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(["--with-cli", "myutils"], tmp_path)
        out = capsys.readouterr().out
        assert "project.scripts" in out or "[project.scripts]" in out

    def test_without_cli_no_shim(self, tmp_path: Path) -> None:
        _run(["myutils"], tmp_path)
        assert not (tmp_path / "scripts" / "myutils.py").exists()


class TestJsonOutput:
    """--json emits parseable JSON listing created files."""

    def test_json_exits_zero(self, tmp_path: Path) -> None:
        rc = _run(["--json", "myutils"], tmp_path)
        assert rc == 0

    def test_json_parses(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(["--json", "myutils"], tmp_path)
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_json_lists_created_files(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(["--json", "myutils"], tmp_path)
        data = json.loads(capsys.readouterr().out)
        files = data.get("files_created", [])
        assert isinstance(files, list)
        assert len(files) >= 3

    def test_json_dry_run_no_writes(self, tmp_path: Path) -> None:
        _run(["--json", "--dry-run", "myutils"], tmp_path)
        assert not (tmp_path / "hephaestus").exists()
