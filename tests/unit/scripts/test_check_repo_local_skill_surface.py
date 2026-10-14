"""Tests for scripts/check_repo_local_skill_surface.py (issue #2810)."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_repo_local_skill_surface.py"
_spec = importlib.util.spec_from_file_location("check_repo_local_skill_surface", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _init_repo(tmp_path: Path) -> Path:
    """Create a tiny Git repository with the minimum root marker."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return tmp_path


def test_find_repo_local_skill_surfaces_returns_empty_list(tmp_path: Path) -> None:
    """A clean repository has no forbidden skill surfaces."""
    repo = _init_repo(tmp_path)

    assert _mod.find_repo_local_skill_surfaces(repo) == []


def test_find_repo_local_skill_surfaces_reports_agents_skills_directory(tmp_path: Path) -> None:
    """A local ``.agents/skills`` directory is rejected."""
    repo = _init_repo(tmp_path)
    (repo / ".agents" / "skills").mkdir(parents=True)

    assert _mod.find_repo_local_skill_surfaces(repo) == [".agents/skills"]


def test_find_repo_local_skill_surfaces_reports_claude_skills_symlink(tmp_path: Path) -> None:
    """A local ``.claude/skills`` symlink is rejected, even when dangling."""
    repo = _init_repo(tmp_path)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "skills").symlink_to(repo / "missing-target")

    assert _mod.find_repo_local_skill_surfaces(repo) == [".claude/skills"]


def test_find_repo_local_skill_surfaces_reports_skills_lock_file(tmp_path: Path) -> None:
    """The retired ``skills-lock.json`` file is rejected."""
    repo = _init_repo(tmp_path)
    (repo / "skills-lock.json").write_text("{}", encoding="utf-8")

    assert _mod.find_repo_local_skill_surfaces(repo) == ["skills-lock.json"]


def test_main_reports_all_forbidden_surfaces(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """main() returns 1 and prints the offending relative paths."""
    repo = _init_repo(tmp_path)
    (repo / ".agents" / "skills").mkdir(parents=True)
    (repo / ".claude").mkdir()
    (repo / ".claude" / "skills").symlink_to(repo / "missing-target")
    (repo / "skills-lock.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(_mod, "get_repo_root", lambda: repo)

    assert _mod.main() == 1
    output = capsys.readouterr().out
    assert "ERROR: repo-local skill surfaces are not allowed." in output
    assert ".agents/skills" in output
    assert ".claude/skills" in output
    assert "skills-lock.json" in output


def test_help_flag_prints_doc_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help must exit 0 and print output for the script smoke test."""
    monkeypatch.setattr(sys, "argv", ["check_repo_local_skill_surface.py", "--help"])

    assert _mod.main() == 0
    assert capsys.readouterr().out.strip()
