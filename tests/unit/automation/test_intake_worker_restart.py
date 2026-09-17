"""Keep worker checkouts outside the reusable intake checkout."""

from pathlib import Path

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation.repo_intake import RepoIntakeError
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.automation.worktree_manager import WorktreeManager
from tests.unit.automation.test_repo_intake import _make_repository, _manager, _run_git


@pytest.mark.precommit
@pytest.mark.parametrize("manager_kind", ["source", "worktree"])
def test_intake_restart_preserves_a_worker_checkout(tmp_path: Path, manager_kind: str) -> None:
    """A preserved worker must not make the next intake fail."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    if manager_kind == "source":
        source = SourceWorkspaceManager(intake.path, repository="repo")
        worker = source.path_for(602, SourceLane.IMPLEMENTATION)
    else:
        worktrees = WorktreeManager(repo_root=intake.path)
        worker = worktrees.base_dir / "auto-602-impl"
    _run_git(intake.path, "worktree", "add", "-b", "existing-pr", str(worker), intake.revision)
    (worker / "preserved.txt").write_text("uncommitted worker data\n", encoding="utf-8")
    before = _run_git(worker, "rev-parse", "HEAD").stdout

    restarted = _manager(caller, remote).prepare()

    assert restarted.path == intake.path
    assert not worker.is_relative_to(intake.path)
    assert _run_git(intake.path, "status", "--porcelain").stdout == ""
    assert _run_git(worker, "rev-parse", "HEAD").stdout == before
    assert _run_git(worker, "branch", "--show-current").stdout.strip() == "existing-pr"
    assert (worker / "preserved.txt").read_text(encoding="utf-8") == "uncommitted worker data\n"


@pytest.mark.precommit
@pytest.mark.parametrize("repository", ["other", "other/repo"])
@pytest.mark.parametrize("explicit_base", [False, True])
def test_intake_worker_rejects_another_repository(
    tmp_path: Path, repository: str, explicit_base: bool
) -> None:
    """Worker ownership must match the repository in the intake receipt."""
    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    base = intake.state_root / "build" / ".worktrees" if explicit_base else None

    with pytest.raises(RepoIntakeError, match="repository"):
        SourceWorkspaceManager(intake.path, repository=repository, base_dir=base)

    assert _run_git(intake.path, "status", "--porcelain").stdout == ""


@pytest.mark.precommit
def test_intake_fixture_does_not_use_the_hook_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fixture Git commands must preserve the caller index and configuration."""
    parent, _ = _make_repository(tmp_path)
    index = parent / ".git" / "index"
    before = index.read_bytes()
    config = parent / ".git" / "config"
    config_before = config.read_bytes()
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.setenv("GIT_INDEX_FILE", str(index))
    monkeypatch.setenv("GIT_DIR", str(parent / ".git"))
    monkeypatch.setenv("GIT_COMMON_DIR", str(parent / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(parent))

    _make_repository(child)

    assert index.read_bytes() == before
    assert config.read_bytes() == config_before
