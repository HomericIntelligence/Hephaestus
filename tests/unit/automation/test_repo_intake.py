"""Behavior tests for the isolated repository-intake control plane."""

from __future__ import annotations

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hephaestus.automation.repo_intake import (
    RepoIntakeError,
    RepoIntakeManager,
    RepoIntakeReceipt,
)
from hephaestus.automation.worktree_snapshot import _controlled_git_env


def _run_git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local Git command for the fixture repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
    )


def _make_repository(tmp_path: Path) -> tuple[Path, Path]:
    """Create a master checkout and a local bare remote with a GitHub URL."""
    remote = tmp_path / "remote.git"
    _run_git(tmp_path, "init", "--bare", "--initial-branch=master", str(remote))
    caller = tmp_path / "caller"
    caller.mkdir()
    _run_git(caller, "init", "--initial-branch=master")
    _run_git(caller, "config", "user.name", "Test User")
    _run_git(caller, "config", "user.email", "test@example.invalid")
    (caller / "tracked.txt").write_text("base\n", encoding="utf-8")
    _run_git(caller, "add", "tracked.txt")
    _run_git(caller, "commit", "-m", "base")
    _run_git(caller, "remote", "add", "origin", str(remote))
    _run_git(caller, "push", "-u", "origin", "master")
    _run_git(caller, "remote", "set-url", "origin", "https://github.com/acme/repo.git")
    return caller, remote


def _manager(caller: Path, remote: Path) -> RepoIntakeManager:
    """Build a manager with a local transport test double."""

    def runner(
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        log_errors: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        del log_errors
        if command[0] == "gh":
            return subprocess.CompletedProcess(command, 0, "master\n", "")
        adjusted = list(command)
        if "fetch" in adjusted:
            adjusted[adjusted.index("origin")] = str(remote)
        return subprocess.run(
            adjusted,
            cwd=cwd,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
            env=env,
        )

    return RepoIntakeManager(
        caller,
        repository="acme/repo",
        gh_command="gh",
        timeout_s=30,
        git_runner=runner,
        git_env=_controlled_git_env(),
        remote_config=(),
    )


def _advance_remote(tmp_path: Path, remote: Path) -> None:
    """Push one fast-forward commit without changing the caller checkout."""
    updater = tmp_path / "updater"
    _run_git(tmp_path, "clone", str(remote), str(updater))
    _run_git(updater, "config", "user.name", "Test User")
    _run_git(updater, "config", "user.email", "test@example.invalid")
    (updater / "remote.txt").write_text("remote\n", encoding="utf-8")
    _run_git(updater, "add", "remote.txt")
    _run_git(updater, "commit", "-m", "remote update")
    _run_git(updater, "push", "origin", "master")


def _rewrite_remote(tmp_path: Path, remote: Path) -> str:
    """Replace the remote default branch with an unrelated commit."""
    rewriter = tmp_path / "rewriter"
    rewriter.mkdir()
    _run_git(rewriter, "init", "--initial-branch=master")
    _run_git(rewriter, "config", "user.name", "Test User")
    _run_git(rewriter, "config", "user.email", "test@example.invalid")
    (rewriter / "rewritten.txt").write_text("rewritten\n", encoding="utf-8")
    _run_git(rewriter, "add", "rewritten.txt")
    _run_git(rewriter, "commit", "-m", "rewrite remote")
    _run_git(rewriter, "remote", "add", "origin", str(remote))
    _run_git(rewriter, "push", "--force", "origin", "master")
    return _run_git(rewriter, "rev-parse", "HEAD").stdout.strip()


def _caller_state(caller: Path) -> tuple[str, str, str, str, str]:
    """Return the caller identity, index, worktree, and status state."""
    return (
        _run_git(caller, "rev-parse", "HEAD").stdout,
        _run_git(caller, "symbolic-ref", "--quiet", "--short", "HEAD").stdout,
        _run_git(caller, "diff", "--cached").stdout,
        _run_git(caller, "diff").stdout,
        _run_git(caller, "status", "--porcelain", "--untracked-files=all").stdout,
    )


def test_isolated_intake_preserves_primary_head_index_and_status(tmp_path: Path) -> None:
    """A tracked and untracked caller stays unchanged while intake syncs."""
    caller, remote = _make_repository(tmp_path)
    before_head = _run_git(caller, "rev-parse", "HEAD").stdout
    (caller / "tracked.txt").write_text("local work\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before_index = _run_git(caller, "diff", "--cached").stdout
    before_status = _run_git(caller, "status", "--porcelain", "--untracked-files=all").stdout

    receipt = _manager(caller, remote).prepare()

    assert receipt.path != caller
    assert receipt.revision == _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip()
    assert _run_git(caller, "rev-parse", "HEAD").stdout == before_head
    assert _run_git(caller, "diff", "--cached").stdout == before_index
    assert (
        _run_git(caller, "status", "--porcelain", "--untracked-files=all").stdout == before_status
    )
    assert (caller / "tracked.txt").read_text(encoding="utf-8") == "local work\n"
    assert (caller / "untracked.txt").read_text(encoding="utf-8") == "keep\n"


def test_isolated_intake_is_bound_to_fetched_default_head(tmp_path: Path) -> None:
    """The receipt and intake HEAD match the fetched remote default branch."""
    caller, remote = _make_repository(tmp_path)
    _advance_remote(tmp_path, remote)

    receipt = _manager(caller, remote).prepare()
    fetched_head = _run_git(remote, "rev-parse", "refs/heads/master").stdout.strip()

    assert receipt.revision == fetched_head
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == fetched_head


def test_receipt_v2_round_trips_manager_owned_state_root(tmp_path: Path) -> None:
    """The receipt identifies its stable durable-state owner directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)

    receipt = manager.prepare()
    payload = receipt.to_dict()

    assert receipt.schema_version == 2
    assert receipt.state_root == manager.state_dir.resolve()
    assert payload["state_root"] == str(manager.state_dir.resolve())
    assert RepoIntakeReceipt.from_dict(payload) == receipt


def test_receipt_v1_fails_closed_as_a_schema_mismatch(tmp_path: Path) -> None:
    """An unreleased receipt schema cannot omit durable-state authority."""
    caller, remote = _make_repository(tmp_path)
    payload = _manager(caller, remote).prepare().to_dict()
    payload["schema_version"] = 1

    with pytest.raises(RepoIntakeError, match="receipt schema mismatch"):
        RepoIntakeReceipt.from_dict(payload)


def test_receipt_rejects_relative_state_root(tmp_path: Path) -> None:
    """Durable-state authority cannot use a relative path."""
    caller, remote = _make_repository(tmp_path)
    payload = _manager(caller, remote).prepare().to_dict()
    payload["state_root"] = "relative-state"

    with pytest.raises(RepoIntakeError, match="receipt values are unsafe"):
        RepoIntakeReceipt.from_dict(payload)


def test_stale_clean_owned_intake_is_rebound_under_common_dir_lock(tmp_path: Path) -> None:
    """A clean owned intake path is reused and rebound under its lock."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    _advance_remote(tmp_path, remote)

    second = _manager(caller, remote).prepare()

    assert second.path == first.path
    assert second.revision != first.revision
    assert second.generation == first.generation + 1
    assert second.revision == _run_git(second.path, "rev-parse", "HEAD").stdout.strip()


def test_direct_scope_from_detached_linked_worktree_prepares_isolated_intake(
    tmp_path: Path,
) -> None:
    """A detached linked caller does not need to move its own worktree."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")
    before_head = _run_git(linked, "rev-parse", "HEAD").stdout

    receipt = _manager(linked, remote).prepare()

    assert receipt.path != linked
    assert _run_git(linked, "rev-parse", "HEAD").stdout == before_head
    assert (
        _run_git(linked, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode == 1
    )


def test_attached_non_default_linked_caller_preserves_all_caller_state(tmp_path: Path) -> None:
    """An attached feature worktree stays byte-for-byte unchanged."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    (linked / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _run_git(linked, "add", "tracked.txt")
    (linked / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (linked / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before = _caller_state(linked)

    receipt = _manager(linked, remote).prepare()

    assert receipt.path != linked
    assert _caller_state(linked) == before


def test_default_branch_already_checked_out_does_not_create_branch_conflict(
    tmp_path: Path,
) -> None:
    """A primary master worktree and detached intake can coexist."""
    caller, remote = _make_repository(tmp_path)

    receipt = _manager(caller, remote).prepare()

    assert _run_git(caller, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == "master"
    assert (
        _run_git(receipt.path, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).returncode
        == 1
    )


@pytest.mark.parametrize(
    "attribute",
    [
        "locked",
        "locked administrative reason",
        "prunable",
        "prunable gitdir file points to non-existent location",
    ],
)
def test_worktree_records_accept_known_attributes_with_optional_reasons(
    tmp_path: Path,
    attribute: str,
) -> None:
    """Known Git worktree attributes can include an optional reason."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    head = _run_git(caller, "rev-parse", "HEAD").stdout.strip()

    def inventory(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        assert command == ["git", "worktree", "list", "--porcelain"]
        output = (
            f"worktree {caller.resolve()}\nHEAD {head}\nbranch refs/heads/master\n{attribute}\n\n"
        )
        return subprocess.CompletedProcess(command, 0, output, "")

    manager._run_command = inventory

    records = manager._worktree_records()

    assert len(records) == 1
    assert records[0].path == caller.resolve()
    assert records[0].head == head
    assert records[0].branch == "refs/heads/master"


def test_dirty_owned_intake_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A dirty owned intake is never removed or silently rebound."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    dirty_file = receipt.path / "operator-notes.txt"
    dirty_file.write_text("keep for recovery\n", encoding="utf-8")
    _advance_remote(tmp_path, remote)

    with pytest.raises(RepoIntakeError, match="dirty and preserved"):
        _manager(caller, remote).prepare()

    assert dirty_file.read_text(encoding="utf-8") == "keep for recovery\n"
    assert receipt.path.is_dir()


def test_foreign_intake_path_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """An unreceipted path cannot be adopted as automation-owned."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.worktree_path.mkdir(parents=True)
    marker = manager.worktree_path / "manual-recovery.txt"
    marker.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError, match=r"unsafe|unowned"):
        manager.prepare()

    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_symlinked_intake_state_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A symlink at the owned state path cannot redirect intake ownership."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    foreign = tmp_path / "foreign-intake"
    foreign.mkdir()
    marker = foreign / "manual-recovery.txt"
    marker.write_text("preserve\n", encoding="utf-8")
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.symlink_to(foreign, target_is_directory=True)

    with pytest.raises(RepoIntakeError, match="state path is unsafe"):
        manager.prepare()

    assert marker.read_text(encoding="utf-8") == "preserve\n"


def test_intake_state_inside_registered_worktree_fails_before_write(tmp_path: Path) -> None:
    """Intake cannot put state inside any registered worktree."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    _run_git(caller, "worktree", "add", "--detach", str(manager.state_parent), "HEAD")
    manager.state_parent.chmod(0o700)
    before = _run_git(manager.state_parent, "status", "--porcelain").stdout

    with pytest.raises(RepoIntakeError, match="overlaps a registered worktree"):
        manager.prepare()

    assert not manager.state_dir.exists()
    assert _run_git(manager.state_parent, "status", "--porcelain").stdout == before


def test_mismatched_receipt_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A receipt with foreign ownership cannot be reused or replaced."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    payload = json.loads(manager.receipt_path.read_text(encoding="utf-8"))
    payload["ownership_key"] = "foreign:owner"
    changed = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    manager.receipt_path.write_text(changed, encoding="utf-8")

    with pytest.raises(RepoIntakeError, match="receipt ownership does not match"):
        manager.prepare()

    assert manager.receipt_path.read_text(encoding="utf-8") == changed
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == receipt.revision


def test_mismatched_state_root_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A receipt cannot redirect durable state outside its owner directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    payload = json.loads(manager.receipt_path.read_text(encoding="utf-8"))
    payload["state_root"] = str((tmp_path / "foreign-state").resolve())
    changed = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    manager.receipt_path.write_text(changed, encoding="utf-8")

    with pytest.raises(RepoIntakeError, match="receipt ownership does not match"):
        manager.prepare()

    assert manager.receipt_path.read_text(encoding="utf-8") == changed
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == receipt.revision


@pytest.mark.parametrize("directory_name", [".automation-state", ".issue_implementer"])
def test_legacy_caller_state_blocks_before_intake_and_is_preserved(
    tmp_path: Path,
    directory_name: str,
) -> None:
    """Legacy caller state requires manual reconciliation before intake."""
    caller, remote = _make_repository(tmp_path)
    source = caller / "build" / directory_name
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b'{"preserve":true}\n')
    manager = _manager(caller, remote)
    destination = manager.state_dir / "build"
    before = _caller_state(caller)

    with pytest.raises(RepoIntakeError, match="legacy state") as caught:
        manager.prepare()

    assert str(source) in str(caught.value)
    assert str(destination) in str(caught.value)
    assert "preserve" in str(caught.value)
    assert "reconcile" in str(caught.value)
    assert marker.read_bytes() == b'{"preserve":true}\n'
    assert not destination.exists()
    assert not manager.worktree_path.exists()
    assert _caller_state(caller) == before


@pytest.mark.parametrize("directory_name", [".automation-state", ".issue_implementer"])
def test_linked_caller_detects_legacy_state_in_primary_worktree(
    tmp_path: Path,
    directory_name: str,
) -> None:
    """A linked caller cannot miss legacy state in its primary worktree."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    source = primary / "build" / directory_name
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b'{"preserve":"primary"}\n')
    manager = _manager(linked, remote)
    destination = manager.state_dir / "build"
    primary_before = _caller_state(primary)
    linked_before = _caller_state(linked)

    with pytest.raises(RepoIntakeError, match="legacy state") as caught:
        manager.prepare()

    assert str(source) in str(caught.value)
    assert str(destination) in str(caught.value)
    assert marker.read_bytes() == b'{"preserve":"primary"}\n'
    assert not destination.exists()
    assert not manager.worktree_path.exists()
    assert _caller_state(primary) == primary_before
    assert _caller_state(linked) == linked_before


@pytest.mark.parametrize("path_kind", ["file", "symlink"])
def test_linked_caller_rejects_unsafe_legacy_path_in_primary_worktree(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """A linked caller validates legacy paths in its primary worktree."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    source = primary / "build" / ".automation-state"
    source.parent.mkdir()
    if path_kind == "file":
        source.write_bytes(b"preserve-primary\n")
    else:
        foreign = tmp_path / "foreign-primary-state"
        foreign.mkdir()
        source.symlink_to(foreign, target_is_directory=True)
    manager = _manager(linked, remote)
    primary_before = _caller_state(primary)
    linked_before = _caller_state(linked)

    with pytest.raises(RepoIntakeError, match="legacy state path is unsafe") as caught:
        manager.prepare()

    assert str(source) in str(caught.value)
    if path_kind == "symlink":
        assert source.is_symlink()
    else:
        assert source.read_bytes() == b"preserve-primary\n"
    assert not manager.worktree_path.exists()
    assert _caller_state(primary) == primary_before
    assert _caller_state(linked) == linked_before


def test_linked_caller_legacy_state_blocks_owned_intake_rebind(tmp_path: Path) -> None:
    """Legacy state in another worktree blocks removal of an owned intake."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    first = _manager(linked, remote).prepare()
    source = primary / "build" / ".automation-state"
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b"preserve-before-rebind\n")
    _advance_remote(tmp_path, remote)

    with pytest.raises(RepoIntakeError, match="legacy state") as caught:
        _manager(linked, remote).prepare()

    assert str(source) in str(caught.value)
    assert marker.read_bytes() == b"preserve-before-rebind\n"
    assert _run_git(first.path, "rev-parse", "HEAD").stdout.strip() == first.revision


def test_linked_caller_reports_all_registered_legacy_state_sources(tmp_path: Path) -> None:
    """One failure identifies legacy state in each registered worktree."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    primary_source = primary / "build" / ".automation-state"
    linked_source = linked / "build" / ".issue_implementer"
    for source, content in (
        (primary_source, b"primary\n"),
        (linked_source, b"linked\n"),
    ):
        source.mkdir(parents=True)
        (source / "state.json").write_bytes(content)
    manager = _manager(linked, remote)

    with pytest.raises(RepoIntakeError, match="legacy state") as caught:
        manager.prepare()

    assert str(primary_source) in str(caught.value)
    assert str(linked_source) in str(caught.value)
    assert (primary_source / "state.json").read_bytes() == b"primary\n"
    assert (linked_source / "state.json").read_bytes() == b"linked\n"
    assert not manager.worktree_path.exists()


def test_empty_legacy_caller_state_does_not_block_intake(tmp_path: Path) -> None:
    """Empty ordinary legacy directories do not claim state authority."""
    caller, remote = _make_repository(tmp_path)
    for directory_name in (".automation-state", ".issue_implementer"):
        (caller / "build" / directory_name).mkdir(parents=True)

    receipt = _manager(caller, remote).prepare()

    assert receipt.path.is_dir()


def test_current_destination_state_is_preserved_during_reuse(tmp_path: Path) -> None:
    """Current durable state does not conflict without legacy caller state."""
    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote).prepare()
    destination = first.state_root / "build" / ".automation-state"
    destination.mkdir(parents=True)
    marker = destination / "current.json"
    marker.write_bytes(b"current\n")

    second = _manager(caller, remote).prepare()

    assert second == first
    assert marker.read_bytes() == b"current\n"


@pytest.mark.parametrize("path_kind", ["file", "symlink"])
def test_unsafe_legacy_state_path_blocks_before_intake(
    tmp_path: Path,
    path_kind: str,
) -> None:
    """A legacy state path must be an ordinary confined directory."""
    caller, remote = _make_repository(tmp_path)
    source = caller / "build" / ".automation-state"
    source.parent.mkdir()
    if path_kind == "file":
        source.write_bytes(b"preserve\n")
    else:
        foreign = tmp_path / "foreign-state"
        foreign.mkdir()
        source.symlink_to(foreign, target_is_directory=True)
    manager = _manager(caller, remote)
    destination = manager.state_dir / "build"
    before = _caller_state(caller)

    with pytest.raises(RepoIntakeError, match="legacy state path is unsafe") as caught:
        manager.prepare()

    assert str(source) in str(caught.value)
    assert str(destination) in str(caught.value)
    assert source.is_symlink() if path_kind == "symlink" else source.read_bytes() == b"preserve\n"
    assert not destination.exists()
    assert not manager.worktree_path.exists()
    assert _caller_state(caller) == before


def test_symlinked_destination_state_path_blocks_before_intake(tmp_path: Path) -> None:
    """A destination state path cannot redirect durable state."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.mkdir(mode=0o700)
    destination_root = manager.state_dir / "build"
    destination_root.mkdir()
    foreign = tmp_path / "foreign-destination"
    foreign.mkdir()
    destination = destination_root / ".automation-state"
    destination.symlink_to(foreign, target_is_directory=True)
    before = _caller_state(caller)

    with pytest.raises(RepoIntakeError, match="destination state path is unsafe") as caught:
        manager.prepare()

    assert str(destination) in str(caught.value)
    assert destination.is_symlink()
    assert not manager.worktree_path.exists()
    assert _caller_state(caller) == before


def test_conflicting_legacy_and_destination_state_is_preserved(tmp_path: Path) -> None:
    """Legacy and destination state cannot be reconciled automatically."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    source = caller / "build" / ".automation-state"
    source.mkdir(parents=True)
    source_marker = source / "source.json"
    source_marker.write_bytes(b"source\n")
    destination = manager.state_dir / "build" / ".issue_implementer"
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.mkdir(mode=0o700)
    destination.mkdir(parents=True)
    destination_marker = destination / "destination.json"
    destination_marker.write_bytes(b"destination\n")
    before = _caller_state(caller)

    with pytest.raises(RepoIntakeError, match="conflicting state") as caught:
        manager.prepare()

    assert str(source) in str(caught.value)
    assert str(manager.state_dir / "build") in str(caught.value)
    assert "preserve" in str(caught.value)
    assert "reconcile" in str(caught.value)
    assert source_marker.read_bytes() == b"source\n"
    assert destination_marker.read_bytes() == b"destination\n"
    assert not manager.worktree_path.exists()
    assert _caller_state(caller) == before


def test_fetch_failure_preserves_attached_caller_state(tmp_path: Path) -> None:
    """A fetch failure does not change the caller branch, index, or files."""
    caller, remote = _make_repository(tmp_path)
    _run_git(caller, "switch", "-c", "feature")
    (caller / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _run_git(caller, "add", "tracked.txt")
    (caller / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before = _caller_state(caller)
    manager = _manager(caller, remote)
    run_command = manager._run_command

    def fail_fetch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "fetch" in command:
            raise subprocess.CalledProcessError(1, command, stderr="fetch failed")
        return run_command(command, **kwargs)

    manager._run_command = fail_fetch

    with pytest.raises(RepoIntakeError, match="fetch failed"):
        manager.prepare()

    assert _caller_state(caller) == before


def test_first_fetch_failure_can_retry_without_adopting_an_unowned_path(tmp_path: Path) -> None:
    """A failed first fetch does not strand the intake path on the next prepare."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    run_command = manager._run_command

    def fail_fetch(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "fetch" in command:
            raise subprocess.CalledProcessError(1, command, stderr="fetch failed")
        return run_command(command, **kwargs)

    manager._run_command = fail_fetch

    with pytest.raises(RepoIntakeError, match="fetch failed"):
        manager.prepare()

    receipt = _manager(caller, remote).prepare()

    assert receipt.path == manager.worktree_path
    assert receipt.revision == _run_git(remote, "rev-parse", "master").stdout.strip()


def test_non_fast_forward_remote_rewrite_preserves_owned_intake(tmp_path: Path) -> None:
    """A rewritten remote cannot replace a clean owned intake worktree."""
    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote).prepare()
    rewritten = _rewrite_remote(tmp_path, remote)
    _run_git(
        caller,
        "fetch",
        "--force",
        str(remote),
        "refs/heads/master:refs/remotes/origin/master",
    )

    with pytest.raises(RepoIntakeError, match="non-fast-forward"):
        _manager(caller, remote).prepare()

    assert rewritten != first.revision
    assert _run_git(first.path, "rev-parse", "HEAD").stdout.strip() == first.revision
    assert (
        json.loads(_manager(caller, remote).receipt_path.read_text(encoding="utf-8"))["revision"]
        == first.revision
    )


def test_concurrent_intake_preparation_reuses_one_owned_path(tmp_path: Path) -> None:
    """Concurrent preparations produce one path and one initial generation."""
    caller, remote = _make_repository(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda _index: _manager(caller, remote).prepare(),
                range(2),
            )
        )

    assert receipts[0].path == receipts[1].path
    assert receipts[0].revision == receipts[1].revision
    assert receipts[0].generation == receipts[1].generation == 1


def test_linked_callers_share_one_concurrent_intake(tmp_path: Path) -> None:
    """Linked callers serialize intake through their shared common directory."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                lambda root: _manager(root, remote).prepare(),
                (caller, linked),
            )
        )

    assert receipts[0].path == receipts[1].path
    assert receipts[0].revision == receipts[1].revision
    assert receipts[0].generation == receipts[1].generation == 1


def test_run_lease_blocks_a_second_process_before_intake_rebind(tmp_path: Path) -> None:
    """A live run keeps a second process from changing its intake checkout."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    _advance_remote(tmp_path, remote)
    child = """
import sys
from pathlib import Path
from hephaestus.automation import git_utils
from hephaestus.automation.repo_intake import RepoIntakeManager
from hephaestus.automation.worktree_snapshot import _controlled_git_env

manager = RepoIntakeManager(
    Path(sys.argv[1]),
    repository="acme/repo",
    gh_command="gh",
    timeout_s=30,
    git_runner=git_utils.run,
    git_env=_controlled_git_env(),
    remote_config=(),
)
try:
    with manager.run_lease():
        raise SystemExit(0)
except Exception as error:
    print(f"{type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(23)
"""

    with manager.run_lease():
        attempted = subprocess.run(
            [sys.executable, "-c", child, str(caller)],
            cwd=caller,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    assert attempted.returncode == 23
    assert "RepoIntakeInUseError: repository_intake_in_use:" in attempted.stderr
    assert "wait for the active automation run to finish" in attempted.stderr
    assert _run_git(first.path, "rev-parse", "HEAD").stdout.strip() == first.revision


def test_run_lease_uses_a_stable_common_directory_path(tmp_path: Path) -> None:
    """Linked callers use one stable lease for their Git common directory."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "lease-linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")
    primary = _manager(caller, remote)
    secondary = _manager(linked, remote)

    assert primary.run_lease_path == secondary.run_lease_path
    with primary.run_lease():
        with pytest.raises(RepoIntakeError) as caught:
            with secondary.run_lease():
                pass
    assert type(caught.value).__name__ == "RepoIntakeInUseError"
