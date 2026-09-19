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


@pytest.mark.precommit
def test_intake_remediation_store_preserves_clean_source(tmp_path: Path) -> None:
    """Host remediation records must not change the intake source tree."""
    from hephaestus.automation.remediation_prepublication import prepublication_private_git_dir

    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    store = prepublication_private_git_dir(repo_root=intake.path, pr_number=603, create=True)

    assert store.is_relative_to(intake.state_root)
    assert not store.is_relative_to(intake.path)
    assert _run_git(intake.path, "status", "--porcelain").stdout == ""
    assert _manager(caller, remote).prepare().path == intake.path


@pytest.mark.precommit
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "review",
        "nested",
        "wrong-item",
        "foreign-git",
        "symlink-root",
        "missing-item",
        "symlink-worker",
    ],
)
def test_intake_worker_host_path_authority(tmp_path: Path, case: str) -> None:
    """Only the verified item path and Git identity supply worker authority."""
    from hephaestus.automation.repo_intake import repository_worker_path_is_valid

    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    root = intake.path
    worker = intake.state_root / "build" / ".worktrees" / "auto-602-impl"
    if case == "review":
        worker = worker.with_name("auto-602-review")
    if case == "nested":
        worker = root / "build" / ".worktrees" / "auto-602-impl"
    if case == "foreign-git":
        worker.mkdir(parents=True)
        _run_git(worker, "init", "--initial-branch=master")
    else:
        _run_git(root, "worktree", "add", "-b", "worker", str(worker), intake.revision)
    if case == "symlink-root":
        root = tmp_path / "root-link"
        root.symlink_to(intake.path, target_is_directory=True)
    if case == "symlink-worker":
        alias = worker.with_name("auto-603-impl")
        alias.symlink_to(worker, target_is_directory=True)
        worker = alias

    admitted = repository_worker_path_is_valid(
        root,
        worker,
        repository="acme/repo",
        item_number=None
        if case == "missing-item"
        else 603
        if case in {"wrong-item", "symlink-worker"}
        else 602,
        lane="review" if case == "review" else "impl",
    )

    assert admitted is (case in {"valid", "review"})


@pytest.mark.precommit
def test_intake_worker_host_path_requires_its_receipt(tmp_path: Path) -> None:
    """Nested paths must not bypass missing intake ownership evidence."""
    from hephaestus.automation.repo_intake import repository_worker_path_is_valid

    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    (intake.state_root / "receipt.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError):
        repository_worker_path_is_valid(
            intake.path, intake.path / "nested", repository="acme/repo", item_number=602
        )


@pytest.mark.precommit
def test_intake_remediation_preserves_legacy_store(tmp_path: Path) -> None:
    """A legacy record requires explicit recovery before new storage is used."""
    from hephaestus.automation.models import DEFAULT_STATE_DIR
    from hephaestus.automation.remediation_prepublication import prepublication_private_git_dir

    caller, remote = _make_repository(tmp_path)
    intake = _manager(caller, remote).prepare()
    legacy = intake.path / DEFAULT_STATE_DIR
    legacy.mkdir(parents=True)
    record = legacy / "preserve.json"
    record.write_text("legacy evidence\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError, match="legacy"):
        prepublication_private_git_dir(repo_root=intake.path, pr_number=603, create=True)

    assert record.read_text(encoding="utf-8") == "legacy evidence\n"
