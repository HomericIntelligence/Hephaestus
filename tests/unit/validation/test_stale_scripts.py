"""Tests for hephaestus.validation.stale_scripts."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.validation.stale_scripts import (
    _is_always_active,
    check_stale_scripts,
    find_stale_scripts,
    get_all_scripts,
    get_reference_targets,
    main,
)


def _write_script(scripts_dir: Path, name: str, content: str = "# placeholder\n") -> Path:
    p = scripts_dir / name
    p.write_text(content)
    return p


def _write_workflow(tmp_path: Path, name: str, content: str) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / name).write_text(content)


class TestIsAlwaysActive:
    """Tests for _is_always_active()."""

    def test_common_py(self) -> None:
        assert _is_always_active("common.py") is True

    def test_conftest(self) -> None:
        assert _is_always_active("conftest.py") is True

    def test_test_prefix(self) -> None:
        assert _is_always_active("test_foo.py") is True

    def test_regular_script(self) -> None:
        assert _is_always_active("bump_version.py") is False


class TestGetAllScripts:
    """Tests for get_all_scripts()."""

    def test_finds_py_files(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "a.py")
        _write_script(scripts_dir, "b.py")
        result = get_all_scripts(scripts_dir)
        assert "a.py" in result
        assert "b.py" in result

    def test_finds_sh_files(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\n")
        result = get_all_scripts(scripts_dir)
        assert "run.sh" in result

    def test_ignores_dotfiles(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / ".hidden.py").write_text("")
        result = get_all_scripts(scripts_dir)
        assert ".hidden.py" not in result

    def test_empty_dir(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        assert get_all_scripts(scripts_dir) == []


class TestGetReferenceTargets:
    """Tests for get_reference_targets()."""

    def test_includes_workflow_yml(self, tmp_path: Path) -> None:
        _write_workflow(tmp_path, "ci.yml", "")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "ci.yml" in names

    def test_includes_justfile(self, tmp_path: Path) -> None:
        (tmp_path / "justfile").write_text("")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "justfile" in names

    def test_includes_precommit(self, tmp_path: Path) -> None:
        (tmp_path / ".pre-commit-config.yaml").write_text("")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert ".pre-commit-config.yaml" in names

    def test_includes_scripts(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "helper.py")
        targets = get_reference_targets(tmp_path)
        names = [t.name for t in targets]
        assert "helper.py" in names

    def test_no_directories(self, tmp_path: Path) -> None:
        targets = get_reference_targets(tmp_path)
        assert all(t.is_file() for t in targets)


class TestFindStaleScripts:
    """Tests for find_stale_scripts()."""

    def test_referenced_script_not_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "my_tool.py")
        _write_workflow(tmp_path, "ci.yml", "run: python scripts/my_tool.py")
        assert find_stale_scripts(tmp_path) == []

    def test_unreferenced_script_is_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        result = find_stale_scripts(tmp_path)
        assert "orphan.py" in result

    def test_always_active_excluded(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "common.py")
        assert find_stale_scripts(tmp_path) == []

    def test_exclude_pattern(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "test_helper.py")
        # Without exclude: test_ is _is_always_active so already excluded
        # Let's use a non-test name
        _write_script(scripts_dir, "old_migration.py")
        result = find_stale_scripts(tmp_path, exclude_pattern="old_migration")
        assert "old_migration.py" not in result

    def test_cross_referenced_script_not_stale(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "helper.py")
        _write_script(scripts_dir, "runner.py", "import helper\n")
        result = find_stale_scripts(tmp_path)
        # helper.py should be found referenced in runner.py
        assert "helper.py" not in result

    def test_no_scripts_dir(self, tmp_path: Path) -> None:
        assert find_stale_scripts(tmp_path) == []

    def test_docs_reference(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "special.py")
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        (docs_dir / "guide.md").write_text("Run `python scripts/special.py` to start.")
        assert find_stale_scripts(tmp_path) == []


class TestCheckStaleScripts:
    """Tests for check_stale_scripts()."""

    def test_no_stale_returns_zero(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "run: python scripts/tool.py")
        assert check_stale_scripts(tmp_path) == 0

    def test_stale_warning_mode_returns_zero(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        assert check_stale_scripts(tmp_path, strict=False) == 0

    def test_stale_strict_mode_returns_one(self, tmp_path: Path) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        assert check_stale_scripts(tmp_path, strict=True) == 1

    def test_verbose_output(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "python scripts/tool.py")
        check_stale_scripts(tmp_path, verbose=True)
        captured = capsys.readouterr()
        assert "Total scripts" in captured.out


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-check-stale-scripts", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_no_stale_exits_zero(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "tool.py")
        _write_workflow(tmp_path, "ci.yml", "python scripts/tool.py")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-stale-scripts", "--repo-root", str(tmp_path)],
        )
        assert main() == 0

    def test_stale_strict_exits_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-check-stale-scripts",
                "--repo-root",
                str(tmp_path),
                "--strict",
            ],
        )
        assert main() == 1

    def test_stale_warning_mode_exits_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scripts_dir = tmp_path / "scripts"
        scripts_dir.mkdir()
        _write_script(scripts_dir, "orphan.py")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-stale-scripts", "--repo-root", str(tmp_path)],
        )
        assert main() == 0
