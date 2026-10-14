"""Tests for scripts/check_build_dir_untracked.py (issue #1214)."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_build_dir_untracked.py"
_spec = importlib.util.spec_from_file_location("check_build_dir_untracked", _SCRIPT)
assert _spec and _spec.loader
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def _init_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return tmp_path


def test_passes_when_no_tracked_build_files(tmp_path: Path) -> None:
    """A clean repo has zero tracked files under build/."""
    repo = _init_repo(tmp_path)
    assert _mod.tracked_build_files(repo) == []


def test_detects_tracked_build_file(tmp_path: Path) -> None:
    """A force-added file under build/ is reported as tracked."""
    repo = _init_repo(tmp_path)
    build = repo / "build"
    build.mkdir()
    (build / "leaked.log").write_text("junk", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "build/leaked.log"], cwd=repo, check=True)
    assert _mod.tracked_build_files(repo) == ["build/leaked.log"]


def test_main_exits_zero_on_clean_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main() returns 0 when nothing is tracked under build/."""
    repo = _init_repo(tmp_path)
    monkeypatch.setattr(_mod, "get_repo_root", lambda: repo)
    assert _mod.main() == 0


def test_help_flag_prints_doc_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help must exit 0 AND print non-empty output (smoke-test contract)."""
    monkeypatch.setattr("sys.argv", ["check_build_dir_untracked.py", "--help"])
    assert _mod.main() == 0
    out = capsys.readouterr().out
    assert out.strip(), "--help produced no output"
