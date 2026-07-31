"""Behavior tests for the bounded repository-evidence helper."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from hephaestus.github.repository_evidence import repository_evidence_main


def _completed(
    stdout: str = "", stderr: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    """Construct a completed subprocess result for adapter mocks."""
    return subprocess.CompletedProcess(["git"], returncode, stdout=stdout, stderr=stderr)


def _git(cwd: Path, *arguments: str) -> str:
    """Run a successful Git command in an isolated test repository."""
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


@patch("hephaestus.github.repository_evidence.run_git")
def test_repository_evidence_collects_a_ten_commit_window_and_matches(
    mock_run_git: MagicMock, capsys
) -> None:
    """The helper reports a bounded history window and Git-native matches."""
    mock_run_git.side_effect = [
        _completed(stdout="newest\noldest\n"),
        _completed(stdout="newest change\noldest base\n"),
        _completed(stdout="parent\n"),
        _completed(stdout=" src/file.py | 1 +\n"),
        _completed(stdout="src/file.py:1:needle\n"),
    ]

    assert repository_evidence_main(["needle", "--source-root", "src"]) == 0

    assert json.loads(capsys.readouterr().out) == {
        "pattern_matches": "src/file.py:1:needle\n",
        "recent_commits": "newest change\noldest base\n",
        "recent_diff": " src/file.py | 1 +\n",
        "recent_range": "parent..HEAD",
    }
    assert mock_run_git.call_args_list[-1].args[0] == [
        "grep",
        "--line-number",
        "-e",
        "needle",
        "--",
        "src",
    ]


@patch("hephaestus.github.repository_evidence.run_git")
def test_repository_evidence_reports_an_unborn_head_without_a_traceback(
    mock_run_git: MagicMock, capsys
) -> None:
    """An unborn repository is a clear operational failure, not a stack trace."""
    mock_run_git.return_value = _completed(stderr="ambiguous argument 'HEAD'", returncode=128)

    assert repository_evidence_main(["needle"]) == 1

    error = capsys.readouterr().err
    assert "cannot resolve HEAD" in error
    assert "Traceback" not in error


def test_repository_evidence_supports_a_sha256_root_commit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Root-commit evidence works with Git's SHA-256 object format."""
    repository = tmp_path / "sha256-repository"
    repository.mkdir()
    _git(repository, "init", "--quiet", "--object-format=sha256")
    _git(repository, "config", "user.email", "tests@example.invalid")
    _git(repository, "config", "user.name", "Hephaestus Tests")
    (repository / "tracked.txt").write_text("needle\n", encoding="utf-8")
    _git(repository, "add", "tracked.txt")
    _git(repository, "commit", "--quiet", "-m", "test: initial")
    monkeypatch.chdir(repository)

    assert repository_evidence_main(["needle"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["pattern_matches"] == "tracked.txt:1:needle\n"
    assert payload["recent_range"].endswith("..HEAD")
