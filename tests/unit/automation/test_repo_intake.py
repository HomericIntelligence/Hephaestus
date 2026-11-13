"""Behavior tests for the isolated repository-intake control plane."""

from __future__ import annotations

import builtins
import errno
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from hephaestus.automation import pipeline_cli
from hephaestus.automation.event_log_retention import event_log_lifecycle
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.coordinator_observability import record_event
from hephaestus.automation.pipeline.repository_lock import RepositoryOperationLock
from hephaestus.automation.repo_intake import (
    RepoIntakeError,
    RepoIntakeManager,
    RepoIntakeReceipt,
)
from hephaestus.automation.worktree_manager import WorktreeManager
from hephaestus.git import runtime as git_runtime
from hephaestus.git.snapshot import _controlled_git_env
from hephaestus.utils.file_lock import file_lock


def _run_git(cwd: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a local Git command for the fixture repository."""
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        env=_controlled_git_env(),
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


def _manager(caller: Path, remote: Path, launch_identity: str | None = None) -> RepoIntakeManager:
    """Build a manager with a local transport test double."""
    identity = uuid4().hex if launch_identity is None else launch_identity

    def runner(
        command: list[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        timeout: int | None = None,
        env: dict[str, str] | None = None,
        log_errors: bool = True,
        input_text: str | None = None,
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
            input=input_text,
        )

    return RepoIntakeManager(
        caller,
        repository="acme/repo",
        gh_command="gh",
        timeout_s=30,
        git_runner=runner,
        git_env=_controlled_git_env(),
        remote_config=(),
        launch_identity=identity,
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


def test_prepare_rejects_a_common_lock_that_changed_after_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Intake does not nest a lock for a newly selected common directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    admitted = WorktreeManager.git_metadata_lock_path(caller)
    foreign = tmp_path / "foreign-git"
    foreign.mkdir()
    selected = foreign / admitted.name
    monkeypatch.setattr(
        "hephaestus.automation.repo_intake.git_metadata_lock_path", lambda _root: selected
    )

    with pytest.raises(RepoIntakeError, match="changed before preparation"):
        manager.prepare(admitted_metadata_lock=admitted)

    assert tuple(foreign.iterdir()) == ()


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


def _caller_state(
    caller: Path,
) -> tuple[str, str, bytes, bytes | None, bytes | None, str, str, str]:
    """Return the caller identity, index, worktree, and status state."""
    raw_index = _run_git(caller, "rev-parse", "--git-path", "index").stdout.strip()
    index_path = Path(raw_index)
    if not index_path.is_absolute():
        index_path = caller / index_path
    tracked = caller / "tracked.txt"
    untracked = caller / "untracked.txt"
    return (
        _run_git(caller, "rev-parse", "HEAD").stdout,
        _run_git(caller, "symbolic-ref", "--quiet", "--short", "HEAD").stdout,
        index_path.read_bytes(),
        tracked.read_bytes() if tracked.is_file() else None,
        untracked.read_bytes() if untracked.is_file() else None,
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


def test_receipt_v3_round_trips_launch_owned_state_root(tmp_path: Path) -> None:
    """The receipt identifies its launch-owned durable-state directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)

    receipt = manager.prepare()
    payload = receipt.to_dict()

    assert receipt.schema_version == 3
    assert receipt.state_root == manager.launch_dir.resolve()
    assert payload["state_root"] == str(manager.launch_dir.resolve())
    assert RepoIntakeReceipt.from_dict(payload) == receipt


def test_receipt_v2_fails_closed_as_a_schema_mismatch(tmp_path: Path) -> None:
    """A permanent-intake receipt cannot adopt launch ownership."""
    caller, remote = _make_repository(tmp_path)
    payload = _manager(caller, remote).prepare().to_dict()
    payload.pop("launch_identity")
    payload["schema_version"] = 2

    with pytest.raises(RepoIntakeError, match="receipt schema mismatch"):
        RepoIntakeReceipt.from_dict(payload)


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


def test_distinct_launches_prepare_distinct_clean_intakes(tmp_path: Path) -> None:
    """Each launch identity owns its own intake bound to the fetched head."""
    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote, "launch-one").prepare()
    _advance_remote(tmp_path, remote)

    second = _manager(caller, remote, "launch-two").prepare()
    fetched_head = _run_git(remote, "rev-parse", "refs/heads/master").stdout.strip()

    assert first.path != second.path
    assert first.launch_identity == "launch-one"
    assert second.launch_identity == "launch-two"
    assert first.revision != second.revision
    assert second.revision == fetched_head
    assert second.generation == 1
    assert _run_git(second.path, "rev-parse", "HEAD").stdout.strip() == second.revision


def test_prepare_rejects_a_duplicate_live_launch_identity(tmp_path: Path) -> None:
    """A held owner lock fails closed instead of sharing a launch directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote, "launch-one")
    receipt: RepoIntakeReceipt | None = None

    with manager.owner():
        receipt = manager.prepare()
        with pytest.raises(RepoIntakeError, match="repository_intake_launch_in_use"):
            _manager(caller, remote, "launch-one").prepare()

    assert str(receipt.path) == str(manager.worktree_path)
    assert manager.receipt_path.exists()


def test_prepare_fails_closed_when_another_process_owns_the_owner_lock(tmp_path: Path) -> None:
    """A foreign owner lock fails closed before any launch preparation."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote, "launch-one")

    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.launch_dir.mkdir(mode=0o700, parents=True)
    foreign_pointer = tmp_path / "foreign-owner.lock"
    foreign_pointer.write_text("", encoding="utf-8")
    foreign_pointer.chmod(0o600)
    with file_lock(foreign_pointer, blocking=False, require_exclusive=True):
        foreign_pointer.rename(manager.owner_lock_path)
        with pytest.raises(RepoIntakeError, match="repository_intake_launch_in_use"):
            manager.prepare()

    assert not manager.worktree_path.exists()
    assert not manager.receipt_path.exists()


@pytest.mark.parametrize("invalid", ["", "UPPER", "with/slash", "with space", "a" * 129])
def test_launch_identity_rejects_unsafe_tokens(tmp_path: Path, invalid: str) -> None:
    """A launch identity cannot carry path separators or oversized tokens."""
    caller, remote = _make_repository(tmp_path)

    with pytest.raises(RepoIntakeError, match="launch identity is invalid"):
        _manager(caller, remote, invalid)


@pytest.mark.parametrize("descendant_is_dirty", [False, True])
def test_cleanup_preserves_a_registered_descendant_worktree(
    tmp_path: Path,
    descendant_is_dirty: bool,
) -> None:
    """Cleanup stops and keeps a registered worktree below the launch path."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    child = first.path / "build" / "registered-child"
    _run_git(caller, "worktree", "add", "--detach", str(child), "HEAD")
    if descendant_is_dirty:
        (child / "tracked.txt").write_text("child work\n", encoding="utf-8")
    child_content = (child / "tracked.txt").read_bytes()
    child_status = _run_git(child, "status", "--porcelain", "--untracked-files=all").stdout
    receipt_content = manager.receipt_path.read_bytes()
    registrations = _run_git(caller, "worktree", "list", "--porcelain").stdout
    caller_state = _caller_state(caller)

    with pytest.raises(RepoIntakeError, match="registered descendant worktree"):
        manager.cleanup()

    assert child.is_dir()
    assert (child / "tracked.txt").read_bytes() == child_content
    assert _run_git(child, "status", "--porcelain", "--untracked-files=all").stdout == child_status
    assert _run_git(caller, "worktree", "list", "--porcelain").stdout == registrations
    assert _run_git(first.path, "rev-parse", "HEAD").stdout.strip() == first.revision
    assert manager.receipt_path.read_bytes() == receipt_content
    assert _caller_state(caller) == caller_state


def test_prepare_rejects_a_foreign_preexisting_launch_worktree(tmp_path: Path) -> None:
    """A foreign worktree at the launch path is preserved and fails closed."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.mkdir(mode=0o700)
    manager.launch_dir.mkdir(mode=0o700, parents=True)
    marker = manager.worktree_path / "manual-recovery.txt"
    marker.parent.mkdir()
    marker.write_text("preserve\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError, match=r"unsafe|unowned"):
        manager.prepare()

    assert marker.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("pointer_kind", ["symlink", "foreign_admin"])
def test_abandoned_intake_rejects_unbound_git_pointer_in_the_sweep(
    tmp_path: Path,
    pointer_kind: str,
) -> None:
    """A damaged abandoned launch tree is preserved, never swept away."""
    caller, remote = _make_repository(tmp_path)
    first_manager = _manager(caller, remote)
    receipt = first_manager.prepare()
    gitfile = receipt.path / ".git"
    receipt_content = first_manager.receipt_path.read_bytes()
    caller_state = _caller_state(caller)
    tracked_content = (receipt.path / "tracked.txt").read_bytes()
    expected_pointer: Path | str
    if pointer_kind == "symlink":
        foreign_pointer = tmp_path / "foreign-git-pointer"
        foreign_pointer.write_text(gitfile.read_text(encoding="utf-8"), encoding="utf-8")
        gitfile.unlink()
        gitfile.symlink_to(foreign_pointer)
        expected_pointer = foreign_pointer
    else:
        foreign_worktree = tmp_path / "foreign-worktree"
        _run_git(caller, "worktree", "add", "--detach", str(foreign_worktree), "HEAD")
        foreign_pointer_content = (foreign_worktree / ".git").read_text(encoding="utf-8")
        gitfile.write_text(foreign_pointer_content, encoding="utf-8")
        expected_pointer = foreign_pointer_content
    manager = _manager(caller, remote, "sweep-launch")
    original_runner = manager._run_command
    damaged_checkout_commands: list[tuple[str, ...]] = []

    def record_intake_git(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if kwargs.get("cwd") == receipt.path:
            damaged_checkout_commands.append(tuple(command))
        return original_runner(command, **kwargs)

    manager._run_command = record_intake_git

    second_receipt = manager.prepare()

    assert damaged_checkout_commands == []
    assert second_receipt.path != receipt.path
    assert first_manager.receipt_path.read_bytes() == receipt_content
    assert (receipt.path / "tracked.txt").read_bytes() == tracked_content
    remaining = _run_git(caller, "worktree", "list", "--porcelain").stdout.split("worktree ")[1:]
    assert any(entry.startswith(str(receipt.path)) for entry in remaining)
    if pointer_kind == "symlink":
        assert gitfile.is_symlink()
        assert gitfile.readlink() == expected_pointer
    else:
        assert gitfile.read_text(encoding="utf-8") == expected_pointer
    assert _caller_state(caller) == caller_state


@pytest.mark.parametrize(
    ("worktree_config_content", "config_is_parseable"),
    [
        pytest.param(
            b'[url "file:///attacker/"]\n\tinsteadOf = https://github.com/\n',
            True,
            id="url-rewrite",
        ),
        pytest.param(
            b'[remote "origin"]\n\tvcs = ext\n',
            True,
            id="remote-vcs",
        ),
        pytest.param(
            b"[core]\n"
            b"\tbare = false # comment\\\n"
            b'[url "file:///attacker/"]\n'
            b"\tinsteadOf = https://github.com/\n",
            True,
            id="comment-backslash-url-rewrite",
        ),
        pytest.param(
            b'[core]\n\tbare = "false\n',
            False,
            id="unterminated-quoted-value",
        ),
    ],
)
def test_abandoned_intake_with_unsafe_worktree_config_is_preserved(
    tmp_path: Path,
    worktree_config_content: bytes,
    config_is_parseable: bool,
) -> None:
    """An abandoned launch with unsafe Git config is preserved by the sweep."""
    caller, remote = _make_repository(tmp_path)
    first_manager = _manager(caller, remote)
    receipt = first_manager.prepare()
    gitfile_line = (receipt.path / ".git").read_text(encoding="utf-8").strip()
    admin_value = Path(gitfile_line.removeprefix("gitdir: "))
    admin_dir = (admin_value if admin_value.is_absolute() else receipt.path / admin_value).resolve(
        strict=True
    )
    _run_git(caller, "config", "extensions.worktreeConfig", "true")
    common_config = receipt.common_dir / "config"
    worktree_config = admin_dir / "config.worktree"
    receipt_content = first_manager.receipt_path.read_bytes()
    common_config_content = common_config.read_bytes()
    caller_state = _caller_state(caller)
    intake_head = _run_git(receipt.path, "rev-parse", "HEAD").stdout
    intake_status = _run_git(
        receipt.path,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ).stdout
    admin_head = (admin_dir / "HEAD").read_bytes()
    admin_index = (admin_dir / "index").read_bytes()
    tracked_content = (receipt.path / "tracked.txt").read_bytes()
    worktree_config.write_bytes(worktree_config_content)

    manager = _manager(caller, remote, "sweep-launch")
    original_runner = manager._run_command
    damaged_checkout_commands: list[tuple[str, ...]] = []

    def record_external_action(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        cwd = kwargs.get("cwd")
        if cwd == receipt.path:
            damaged_checkout_commands.append(tuple(command))
        return original_runner(command, **kwargs)

    manager._run_command = record_external_action

    second_receipt = manager.prepare()

    assert damaged_checkout_commands == []
    assert second_receipt.path != receipt.path
    assert first_manager.receipt_path.read_bytes() == receipt_content
    assert common_config.read_bytes() == common_config_content
    assert worktree_config.read_bytes() == worktree_config_content
    remaining = _run_git(caller, "worktree", "list", "--porcelain").stdout.split("worktree ")[1:]
    assert any(entry.startswith(str(receipt.path)) for entry in remaining)
    assert (admin_dir / "HEAD").read_bytes() == admin_head
    assert (admin_dir / "index").read_bytes() == admin_index
    if config_is_parseable:
        assert _run_git(receipt.path, "rev-parse", "HEAD").stdout == intake_head
        assert (
            _run_git(
                receipt.path,
                "status",
                "--porcelain",
                "--untracked-files=all",
            ).stdout
            == intake_status
        )
    assert (receipt.path / "tracked.txt").read_bytes() == tracked_content
    assert _caller_state(caller) == caller_state


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


def test_dirty_abandoned_intake_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A dirty abandoned intake is never removed or silently rebound."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    dirty_file = receipt.path / "operator-notes.txt"
    dirty_file.write_text("keep for recovery\n", encoding="utf-8")
    _advance_remote(tmp_path, remote)

    second = _manager(caller, remote, "next-launch").prepare()

    assert dirty_file.read_text(encoding="utf-8") == "keep for recovery\n"
    assert receipt.path.is_dir()
    assert second.path != receipt.path


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

    with pytest.raises(RepoIntakeError, match="overlaps a registered worktree"):
        manager.prepare()

    # No intake authority was written; only the empty lock skeleton remains.
    assert not manager.receipt_path.exists()
    assert not manager.registration_path.exists()
    assert not manager.worktree_path.exists()
    residue = _run_git(manager.state_parent, "status", "--porcelain").stdout.splitlines()
    assert residue == [f"?? {manager.state_dir.name}/"]


def test_launch_receipt_tamper_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A receipt with foreign ownership cannot be reused or replaced."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    payload = json.loads(manager.receipt_path.read_text(encoding="utf-8"))
    payload["ownership_key"] = "foreign:owner"
    changed = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    manager.receipt_path.write_text(changed, encoding="utf-8")

    with pytest.raises(RepoIntakeError, match="unowned repository-intake worktree"):
        manager.prepare()

    assert manager.receipt_path.read_text(encoding="utf-8") == changed
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == receipt.revision


def test_launch_receipt_state_root_tamper_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    """A receipt cannot redirect durable state outside its owner directory."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    receipt = manager.prepare()
    payload = json.loads(manager.receipt_path.read_text(encoding="utf-8"))
    payload["state_root"] = str((tmp_path / "foreign-state").resolve())
    changed = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    manager.receipt_path.write_text(changed, encoding="utf-8")

    with pytest.raises(RepoIntakeError, match="unowned repository-intake worktree"):
        manager.prepare()

    assert manager.receipt_path.read_text(encoding="utf-8") == changed
    assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == receipt.revision


def test_launch_cleanup_preserves_durable_build_state(tmp_path: Path) -> None:
    """Current durable state at the state root survives the launch cleanup."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.prepare()
    destination = manager.state_dir / "build" / ".automation-state"
    destination.mkdir(parents=True)
    marker = destination / "current.json"
    marker.write_bytes(b"current\n")

    manager.cleanup()

    assert marker.read_bytes() == b"current\n"
    assert not manager.launch_dir.exists()


def test_launch_cleanup_is_idempotent(tmp_path: Path) -> None:
    """A repeated cleanup reports nothing left to remove."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.prepare()

    manager.cleanup()

    assert not manager.launch_dir.exists()
    manager.cleanup()
    assert not manager.launch_dir.exists()


def test_launch_cleanup_preserves_dirty_launch_intake(tmp_path: Path) -> None:
    """A dirty launch worktree is preserved with the retained path."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    dirty_file = first.path / "operator-notes.txt"
    dirty_file.write_text("keep for recovery\n", encoding="utf-8")

    with pytest.raises(RepoIntakeError, match=f"preserved.*{re.escape(str(first.path))}"):
        manager.cleanup()

    assert dirty_file.read_text(encoding="utf-8") == "keep for recovery\n"
    assert first.path.is_dir()


def test_launch_cleanup_removes_the_git_worktree_registration(tmp_path: Path) -> None:
    """Cleanup removes both the launch tree and its Git worktree registration."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    assert str(first.path) in _run_git(caller, "worktree", "list", "--porcelain").stdout

    manager.cleanup()

    assert str(first.path) not in _run_git(caller, "worktree", "list", "--porcelain").stdout
    assert not manager.launch_dir.exists()


@pytest.mark.parametrize("directory_name", [".automation-state", Path(DEFAULT_STATE_DIR).name])
def test_legacy_caller_state_migrates_into_the_intake_root(
    tmp_path: Path,
    directory_name: str,
) -> None:
    """Durable state in the caller migrates instead of blocking intake."""
    caller, remote = _make_repository(tmp_path)
    source = caller / "build" / directory_name
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b'{"migrated":true}\n')
    manager = _manager(caller, remote)
    destination = manager.state_dir / "build" / directory_name

    manager.prepare()

    assert (destination / "state.json").read_bytes() == b'{"migrated":true}\n'
    assert manager.worktree_path.exists()


def test_dirty_caller_checkout_does_not_block_intake(tmp_path: Path) -> None:
    """A dirty caller checkout is unrelated local work and must not block."""
    caller, remote = _make_repository(tmp_path)
    (caller / "tracked.txt").write_text("local work\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    manager = _manager(caller, remote)

    manager.prepare()

    assert manager.worktree_path.exists()
    assert (caller / "tracked.txt").read_text(encoding="utf-8") == "local work\n"
    assert (caller / "untracked.txt").read_text(encoding="utf-8") == "keep\n"


def test_modified_tracked_files_in_a_worktree_do_not_block_intake(tmp_path: Path) -> None:
    """Another agent's uncommitted work in a registered worktree is left alone."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    (linked / "tracked.txt").write_text("agent work\n", encoding="utf-8")
    manager = _manager(caller, remote)

    manager.prepare()

    assert manager.worktree_path.exists()
    assert (linked / "tracked.txt").read_text(encoding="utf-8") == "agent work\n"


@pytest.mark.parametrize("stale_registration", [False, True])
def test_compatibility_lock_does_not_poison_intake_validation(
    tmp_path: Path, stale_registration: bool
) -> None:
    """The caller-local compatibility lock is not durable caller state."""
    caller, remote = _make_repository(tmp_path)
    stale = tmp_path / "removed-worktree"
    if stale_registration:
        _run_git(caller, "worktree", "add", "--detach", str(stale), "HEAD")
        shutil.rmtree(stale)
    manager = _manager(caller, remote)
    lock_path = caller / DEFAULT_STATE_DIR / "locks" / "git-repo.lock"
    compatibility_paths = (
        lock_path,
        Path(f"{lock_path}.owner.lock"),
        Path(f"{lock_path}.owner.json"),
    )
    lock = RepositoryOperationLock("repo", lock_path=lock_path)

    manager.validate(operational_state_paths=compatibility_paths)
    with (
        git_runtime.operation_deadline(time.monotonic() + 30),
        lock.acquire(operation="prepare_intake", timeout_s=30),
    ):
        receipt = manager.prepare(operational_state_paths=compatibility_paths)

    assert receipt.path.is_dir()
    assert lock_path.is_file()
    assert Path(f"{lock_path}.owner.lock").is_file()
    manager.validate(operational_state_paths=compatibility_paths)
    if stale_registration:
        assert not stale.exists()
        assert str(stale) in _run_git(caller, "worktree", "list", "--porcelain").stdout


@pytest.mark.parametrize("failure", [PermissionError, RuntimeError])
def test_unreadable_registration_still_blocks_compatibility_locks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    """An inaccessible registration must not be treated as an absent worktree."""
    caller, remote = _make_repository(tmp_path)
    sibling = tmp_path / "sibling"
    _run_git(caller, "worktree", "add", "--detach", str(sibling), "HEAD")
    manager = _manager(caller, remote)
    lock_path = caller / DEFAULT_STATE_DIR / "locks" / "git-repo.lock"
    compatibility_paths = (
        lock_path,
        Path(f"{lock_path}.owner.lock"),
        Path(f"{lock_path}.owner.json"),
    )
    original_resolve = Path.resolve

    def resolve(path: Path, strict: bool = False) -> Path:
        if path == sibling and strict:
            raise failure("registration unavailable")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(RepoIntakeError, match="worktree registration is unavailable") as caught:
        manager.validate(operational_state_paths=compatibility_paths)

    assert isinstance(caught.value.__cause__, failure)
    assert not manager.worktree_path.exists()
    assert sibling.is_dir()


@pytest.mark.parametrize("unsafe_kind", ["symlink", "mode"])
def test_compatibility_lock_cannot_hide_unsafe_caller_state(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    """Only safe files from the exact compatibility lock can be ignored."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    lock_path = caller / DEFAULT_STATE_DIR / "locks" / "git-repo.lock"
    owner_lock_path = Path(f"{lock_path}.owner.lock")
    compatibility_paths = (
        lock_path,
        owner_lock_path,
        Path(f"{lock_path}.owner.json"),
    )
    lock = RepositoryOperationLock("repo", lock_path=lock_path)
    with lock.acquire(operation="prepare_intake", timeout_s=30):
        pass

    if unsafe_kind == "symlink":
        owner_lock_path.unlink()
        unsafe_path = owner_lock_path
        unsafe_path.symlink_to(tmp_path / "foreign-lock")
    else:
        unsafe_path = lock_path
        unsafe_path.chmod(0o644)

    with pytest.raises(RepoIntakeError):
        manager.validate(operational_state_paths=compatibility_paths)

    assert unsafe_path.exists() or unsafe_path.is_symlink()


def test_extra_untracked_caller_state_migrates_through_a_compatibility_lock(
    tmp_path: Path,
) -> None:
    """An extra untracked file in the lock directory is copied, not a failure."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    lock_path = caller / DEFAULT_STATE_DIR / "locks" / "git-repo.lock"
    compatibility_paths = (
        lock_path,
        Path(f"{lock_path}.owner.lock"),
        Path(f"{lock_path}.owner.json"),
    )
    lock = RepositoryOperationLock("repo", lock_path=lock_path)
    with lock.acquire(operation="prepare_intake", timeout_s=30):
        pass
    extra_path = lock_path.parent / "legacy-state.json"
    extra_path.write_text("preserve\n", encoding="utf-8")
    destination = manager.state_dir / DEFAULT_STATE_DIR / "locks" / "legacy-state.json"

    manager.validate(operational_state_paths=compatibility_paths)

    assert destination.read_text(encoding="utf-8") == "preserve\n"
    assert extra_path.read_text(encoding="utf-8") == "preserve\n"


@pytest.mark.parametrize("directory_name", [".automation-state", Path(DEFAULT_STATE_DIR).name])
def test_linked_caller_migrates_primary_worktree_state(
    tmp_path: Path,
    directory_name: str,
) -> None:
    """A linked caller migrates durable state in its primary worktree."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    source = primary / "build" / directory_name
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b'{"migrated":"primary"}\n')
    manager = _manager(linked, remote)
    destination = manager.state_dir / "build" / directory_name

    receipt = manager.prepare()

    assert (destination / "state.json").read_bytes() == b'{"migrated":"primary"}\n'
    assert marker.read_bytes() == b'{"migrated":"primary"}\n'
    assert receipt.path.is_dir()


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


def test_linked_caller_primary_state_migrates_before_owned_intake_rebind(
    tmp_path: Path,
) -> None:
    """Primary state migrates, and the owned intake is still rebound."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    first = _manager(linked, remote).prepare()
    source = primary / "build" / ".automation-state"
    source.mkdir(parents=True)
    marker = source / "state.json"
    marker.write_bytes(b"migrate-before-rebind\n")
    _advance_remote(tmp_path, remote)
    manager = _manager(linked, remote)

    second = manager.prepare()

    assert (manager.state_dir / "build" / ".automation-state" / "state.json").read_bytes() == (
        b"migrate-before-rebind\n"
    )
    assert marker.read_bytes() == b"migrate-before-rebind\n"
    assert second.revision != first.revision
    assert _run_git(second.path, "rev-parse", "HEAD").stdout.strip() == second.revision


def test_linked_caller_migrates_state_from_every_registered_worktree(tmp_path: Path) -> None:
    """Each registered worktree keeps a copy of its state in the intake root."""
    primary, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(primary, "worktree", "add", "-b", "feature", str(linked), "HEAD")
    primary_source = primary / "build" / ".automation-state"
    linked_source = linked / DEFAULT_STATE_DIR
    for source, name, content in (
        (primary_source, "primary.json", b"primary\n"),
        (linked_source, "linked.json", b"linked\n"),
    ):
        source.mkdir(parents=True)
        (source / name).write_bytes(content)
    manager = _manager(linked, remote)
    primary_destination = manager.state_dir / "build" / ".automation-state" / "primary.json"
    linked_destination = manager.state_dir / "build" / Path(DEFAULT_STATE_DIR).name / "linked.json"

    receipt = manager.prepare()

    assert primary_destination.read_bytes() == b"primary\n"
    assert linked_destination.read_bytes() == b"linked\n"
    assert (primary_source / "primary.json").read_bytes() == b"primary\n"
    assert (linked_source / "linked.json").read_bytes() == b"linked\n"
    assert receipt.path.is_dir()


def test_empty_legacy_caller_state_does_not_block_intake(tmp_path: Path) -> None:
    """Empty ordinary legacy directories do not claim state authority."""
    caller, remote = _make_repository(tmp_path)
    for directory_name in (".automation-state", Path(DEFAULT_STATE_DIR).name):
        (caller / "build" / directory_name).mkdir(parents=True)

    receipt = _manager(caller, remote).prepare()

    assert receipt.path.is_dir()


@pytest.mark.parametrize(
    "writer_relative",
    (Path("build/.worktrees/writer"), Path("writer")),
    ids=("automation-worktree", "direct-child-worktree"),
)
def test_nested_registered_writer_event_log_does_not_block_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    writer_relative: Path,
) -> None:
    """Pre-intake diagnostics stay outside a registered writer worktree."""
    caller, remote = _make_repository(tmp_path)
    # A normal home is outside the inferred projects root. Keep the fixture
    # independent of an ambient system temporary namespace.
    user_home = tmp_path.with_name(f"{tmp_path.name}-user-home")
    user_home.mkdir()
    monkeypatch.setattr(
        Path,
        "home",
        classmethod(lambda cls: user_home),
    )
    writer = caller / writer_relative
    _run_git(caller, "worktree", "add", "--detach", str(writer), "HEAD")
    monkeypatch.chdir(writer)
    config = pipeline_cli.build_config(
        pipeline_cli.parse_args([]),
        "acme",
        ["repo"],
    )
    assert config.event_log_path is not None
    coordinator = SimpleNamespace(
        config=config,
        event_log=[],
        _event_log_disabled=False,
    )

    with event_log_lifecycle(
        config.event_log_path,
        retention_days=30,
        retention_count=100,
        dry_run=False,
    ):
        record_event(
            coordinator,
            "repo_submit",
            "repo#1",
            now_fn=lambda: 0.0,
            logger=logging.getLogger(__name__),
        )

    diagnostics_dir = config.event_log_path.parent
    assert config.event_log_path.is_file()
    assert (diagnostics_dir / f".{config.event_log_path.name}.lock").is_file()
    assert (diagnostics_dir / ".pipeline-events-retention.lock").is_file()
    for registered_root in (caller.resolve(), writer.resolve()):
        assert registered_root not in config.event_log_path.resolve().parents

    receipt = _manager(writer, remote).prepare()

    assert receipt.path.is_dir()


def test_registered_home_event_log_does_not_dirty_or_block_intake(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-intake diagnostics do not dirty a home directory that is a worktree."""
    caller, remote = _make_repository(tmp_path)
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: caller))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))
    monkeypatch.chdir(caller)
    config = pipeline_cli.build_config(
        pipeline_cli.parse_args(["--projects-dir", str(projects_dir)]),
        "acme",
        ["repo"],
    )
    assert config.event_log_path is not None
    coordinator = SimpleNamespace(
        config=config,
        event_log=[],
        _event_log_disabled=False,
    )

    with event_log_lifecycle(
        config.event_log_path,
        retention_days=30,
        retention_count=100,
        dry_run=False,
    ):
        record_event(
            coordinator,
            "repo_submit",
            "repo#1",
            now_fn=lambda: 0.0,
            logger=logging.getLogger(__name__),
        )

    assert config.event_log_path.parent == (
        host_temp
        / f"hephaestus-{os.geteuid()}"
        / ".hephaestus-diagnostics"
        / config.projects_dir.name
    )
    assert config.event_log_path.is_file()
    status = _run_git(
        caller,
        "status",
        "--porcelain",
        "--untracked-files=all",
    ).stdout
    assert status == ""
    assert _manager(caller, remote).prepare().path.is_dir()


def test_current_destination_state_is_preserved_during_reuse(tmp_path: Path) -> None:
    """Current durable state does not conflict without legacy caller state."""
    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote, "state-launch").prepare()
    destination = first.state_root.parent.parent / "build" / ".automation-state"
    destination.mkdir(parents=True)
    marker = destination / "current.json"
    marker.write_bytes(b"current\n")

    second = _manager(caller, remote, "next-state-launch").prepare()

    assert second != first
    assert second.revision == first.revision
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


def test_destination_state_wins_over_a_caller_state_directory(tmp_path: Path) -> None:
    """An existing entry in the destination is not overwritten by a copy."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    source = caller / "build" / ".automation-state"
    source.mkdir(parents=True)
    source_marker = source / "source.json"
    source_marker.write_bytes(b"source\n")
    destination = manager.state_dir / DEFAULT_STATE_DIR
    manager.state_parent.mkdir(mode=0o700, parents=True)
    manager.state_dir.mkdir(mode=0o700)
    destination.mkdir(parents=True)
    destination_marker = destination / "source.json"
    destination_marker.write_bytes(b"destination\n")

    receipt = manager.prepare()

    assert destination_marker.read_bytes() == b"destination\n"
    assert source_marker.read_bytes() == b"source\n"
    assert receipt.path.is_dir()


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
    """A failed first fetch rolls back and a retry gets a fresh launch intake."""
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

    assert not manager.receipt_path.exists()
    assert not manager.worktree_path.exists()
    assert (
        str(manager.worktree_path) not in _run_git(caller, "worktree", "list", "--porcelain").stdout
    )

    receipt = _manager(caller, remote, "retry-launch").prepare()

    assert receipt.revision == _run_git(remote, "rev-parse", "master").stdout.strip()


def test_next_launch_prepares_isolated_intake_after_a_remote_rewrite(tmp_path: Path) -> None:
    """A rewritten remote never pins a previous launch's stale revision."""
    caller, remote = _make_repository(tmp_path)
    first = _manager(caller, remote, "before-rewrite").prepare()
    rewritten = _rewrite_remote(tmp_path, remote)
    _run_git(
        caller,
        "fetch",
        "--force",
        str(remote),
        "refs/heads/master:refs/remotes/origin/master",
    )

    second = _manager(caller, remote, "after-rewrite").prepare()

    assert rewritten != first.revision
    assert second.revision == rewritten
    assert second.revision == _run_git(second.path, "rev-parse", "HEAD").stdout.strip()


def test_concurrent_intake_preparations_use_distinct_launch_intakes(tmp_path: Path) -> None:
    """Concurrent preparations keep distinct launch paths under one lock."""
    caller, remote = _make_repository(tmp_path)
    managers = [_manager(caller, remote, f"launch-{index}") for index in range(2)]

    with ExitStack() as stack:
        for manager in managers:
            stack.enter_context(manager.owner())
        with ThreadPoolExecutor(max_workers=2) as executor:
            receipts = list(executor.map(lambda manager: manager.prepare(), managers))

    assert receipts[0].path != receipts[1].path
    assert receipts[0].revision == receipts[1].revision
    assert receipts[0].generation == receipts[1].generation == 1
    for receipt in receipts:
        assert _run_git(receipt.path, "rev-parse", "HEAD").stdout.strip() == receipt.revision


def test_duplicate_launch_identity_fails_closed_under_contention(tmp_path: Path) -> None:
    """A duplicate launch identity fails closed instead of sharing an intake."""
    caller, remote = _make_repository(tmp_path)
    failures: list[BaseException] = []
    acquired: list[RepoIntakeManager] = []
    start = threading.Barrier(2)
    # A network mount can delay flock visibility between clients, so the
    # contender retries its nonblocking acquisition within a bounded window.
    deadline = time.monotonic() + 5.0

    def hold(manager: RepoIntakeManager) -> None:
        with manager.owner():
            acquired.append(manager)
            start.wait(timeout=10)
            time.sleep(0.2)

    def contend(manager: RepoIntakeManager) -> None:
        start.wait(timeout=10)
        while time.monotonic() < deadline:
            try:
                with manager.owner():
                    acquired.append(manager)
                    return
            except RepoIntakeError as exc:
                failures.append(exc)
                return
        failures.append(TimeoutError("owner-lock contention was never observed"))

    managers = [_manager(caller, remote, "same-launch") for _ in range(2)]
    # Pre-create the lock file so both threads open one inode; a network
    # mount can hand two concurrent O_CREAT opens distinct inodes.
    managers[0].state_parent.mkdir(mode=0o700, parents=True)
    managers[0].launch_dir.mkdir(mode=0o700, parents=True)
    managers[0].owner_lock_path.touch(mode=0o600)
    holder = threading.Thread(target=hold, args=(managers[0],))
    contender = threading.Thread(target=contend, args=(managers[1],))
    holder.start()
    contender.start()
    holder.join(timeout=30)
    contender.join(timeout=30)
    assert not contender.is_alive()

    assert len(acquired) == 1
    assert len(failures) == 1
    assert "launch_in_use" in str(failures[0])


def test_linked_callers_prepare_distinct_launch_intakes(tmp_path: Path) -> None:
    """Linked callers serialize metadata and keep their own launch intakes."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")

    def prepare(pair: tuple[Path, str]) -> RepoIntakeReceipt:
        manager = _manager(pair[0], remote, pair[1])
        with manager.owner():
            return manager.prepare()

    with ThreadPoolExecutor(max_workers=2) as executor:
        receipts = list(
            executor.map(
                prepare,
                ((caller, "primary-launch"), (linked, "linked-launch")),
            )
        )

    assert receipts[0].path != receipts[1].path
    assert receipts[0].revision == receipts[1].revision
    assert receipts[0].generation == receipts[1].generation == 1


def test_linked_processes_run_distinct_launches_across_processes(tmp_path: Path) -> None:
    """Linked processes serialize metadata and keep their own launch intakes."""
    pytest.importorskip("fcntl")
    source_root = Path(__file__).resolve().parents[3]
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "linked-process-caller"
    _run_git(caller, "worktree", "add", "-b", "linked-feature", str(linked), "HEAD")
    caller_before = _caller_state(caller)
    linked_before = _caller_state(linked)
    child = r"""
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

from hephaestus.git.runtime import operation_deadline
from hephaestus.automation.repo_intake import RepoIntakeManager
from hephaestus.git.snapshot import _controlled_git_env

caller = Path(sys.argv[1])
remote = Path(sys.argv[2])
mode = sys.argv[3]
ready = Path(sys.argv[4])
release = Path(sys.argv[5])
blocked = False

def runner(command, *, cwd=None, check=True, timeout=None, env=None,
           log_errors=True, input_text=None):
    global blocked
    del log_errors
    if command[0] == "gh":
        return subprocess.CompletedProcess(command, 0, "master\n", "")
    if mode == "hold" and command[:3] == ["git", "remote", "get-url"] and not blocked:
        blocked = True
        ready.write_text("lock-owned\n", encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while not release.is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("parent did not release the lock owner")
            time.sleep(0.01)
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
        input=input_text,
    )

manager = RepoIntakeManager(
    caller,
    repository="acme/repo",
    gh_command="gh",
    timeout_s=30,
    git_runner=runner,
    git_env=_controlled_git_env(),
    remote_config=(),
    launch_identity=uuid.uuid4().hex,
)
if mode == "timeout":
    ready.write_text("attempting\n", encoding="utf-8")
    try:
        with operation_deadline(time.monotonic() + 0.5):
            manager.prepare()
    except subprocess.TimeoutExpired:
        print("lock-timeout")
        raise SystemExit(23)
    raise SystemExit("contending preparation did not time out")
print(json.dumps(manager.prepare().to_dict(), sort_keys=True))
"""
    owner_ready = tmp_path / "owner.ready"
    contender_ready = tmp_path / "contender.ready"
    release = tmp_path / "release"
    processes: list[subprocess.Popen[str]] = []
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)

    def start(root: Path, mode: str, ready: Path) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                child,
                str(root),
                str(remote),
                mode,
                str(ready),
                str(release),
            ],
            cwd=source_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        processes.append(process)
        return process

    def wait_for(path: Path, message: str) -> None:
        deadline = time.monotonic() + 10.0
        while not path.is_file():
            if time.monotonic() >= deadline:
                pytest.fail(message)
            time.sleep(0.01)

    try:
        owner = start(caller, "hold", owner_ready)
        wait_for(owner_ready, "The first process did not acquire the metadata lock.")
        contender = start(linked, "timeout", contender_ready)
        wait_for(contender_ready, "The second process did not attempt preparation.")
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
        assert contender.returncode == 23, contender_stderr
        assert contender_stdout.strip() == "lock-timeout"
        assert owner.poll() is None

        release.write_text("release\n", encoding="utf-8")
        owner_stdout, owner_stderr = owner.communicate(timeout=30)
        assert owner.returncode == 0, owner_stderr

        retry = start(linked, "normal", tmp_path / "unused.ready")
        retry_stdout, retry_stderr = retry.communicate(timeout=30)
        assert retry.returncode == 0, retry_stderr
        owner_receipt = json.loads(owner_stdout)
        retry_receipt = json.loads(retry_stdout)
        assert owner_receipt["path"] != retry_receipt["path"]
        assert owner_receipt["revision"] == retry_receipt["revision"]
        assert owner_receipt["generation"] == retry_receipt["generation"] == 1
        assert _caller_state(caller) == caller_before
        assert _caller_state(linked) == linked_before
    finally:
        release.touch(exist_ok=True)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


@pytest.mark.parametrize("failure_type", [InterruptedError, subprocess.TimeoutExpired])
def test_preparation_command_stop_preserves_caller_state(
    tmp_path: Path,
    failure_type: type[BaseException],
) -> None:
    """A stop during worktree creation leaves caller state unchanged."""
    caller, remote = _make_repository(tmp_path)
    _run_git(caller, "switch", "-c", "feature")
    (caller / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _run_git(caller, "add", "tracked.txt")
    (caller / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before = _caller_state(caller)
    manager = _manager(caller, remote)
    run_command = manager._run_command

    def stop_add(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if "worktree" in command and "add" in command:
            if failure_type is subprocess.TimeoutExpired:
                raise subprocess.TimeoutExpired(command, 1)
            raise InterruptedError("injected preparation stop")
        return run_command(command, **kwargs)

    manager._run_command = stop_add

    with pytest.raises(failure_type):
        manager.prepare()

    assert _caller_state(caller) == before
    assert not manager.worktree_path.exists()
    assert not manager.receipt_path.exists()


def test_running_worktree_add_interruption_preserves_caller_state(tmp_path: Path) -> None:
    """A signal during a live worktree-add process preserves caller state."""
    pytest.importorskip("fcntl")
    source_root = Path(__file__).resolve().parents[3]
    caller, remote = _make_repository(tmp_path)
    _run_git(caller, "switch", "-c", "feature")
    (caller / "tracked.txt").write_text("staged\n", encoding="utf-8")
    _run_git(caller, "add", "tracked.txt")
    (caller / "tracked.txt").write_text("unstaged\n", encoding="utf-8")
    (caller / "untracked.txt").write_text("keep\n", encoding="utf-8")
    before = _caller_state(caller)
    marker = tmp_path / "worktree-add.started"
    child = r"""
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from hephaestus.git.runtime import operation_deadline
from hephaestus.automation.repo_intake import RepoIntakeManager
from hephaestus.git.snapshot import _controlled_git_env

caller = Path(sys.argv[1])
remote = Path(sys.argv[2])
marker = Path(sys.argv[3])
shutdown = threading.Event()
signal.signal(signal.SIGUSR1, lambda _signum, _frame: shutdown.set())

def runner(command, *, cwd=None, check=True, timeout=None, env=None,
           log_errors=True, input_text=None):
    del log_errors
    if command[0] == "gh":
        return subprocess.CompletedProcess(command, 0, "master\n", "")
    adjusted = list(command)
    if "fetch" in adjusted:
        adjusted[adjusted.index("origin")] = str(remote)
    if "worktree" in adjusted and "add" in adjusted:
        command_shim = r'''
import os
import signal
import sys
from pathlib import Path

Path(sys.argv[1]).write_text(f"{os.getpid()}\n", encoding="utf-8")
while True:
    signal.pause()
'''
        process = subprocess.Popen(
            [sys.executable, "-c", command_shim, str(marker)],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=True,
        )
        try:
            while not shutdown.wait(0.01):
                pass
            raise InterruptedError("injected live-command interruption")
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    return subprocess.run(
        adjusted,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
        env=env,
        input=input_text,
    )

manager = RepoIntakeManager(
    caller,
    repository="acme/repo",
    gh_command="gh",
    timeout_s=30,
    git_runner=runner,
    git_env=_controlled_git_env(),
    remote_config=(),
    launch_identity=uuid.uuid4().hex,
)
try:
    with operation_deadline(time.monotonic() + 30.0, shutdown=shutdown):
        manager.prepare()
except InterruptedError:
    raise SystemExit(23)
raise SystemExit("preparation did not observe interruption")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    process = subprocess.Popen(
        [sys.executable, "-c", child, str(caller), str(remote), str(marker)],
        cwd=source_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        deadline = time.monotonic() + 10.0
        while not marker.is_file():
            if process.poll() is not None:
                _stdout, stderr = process.communicate()
                pytest.fail(f"Preparation stopped before worktree add: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail("Preparation did not start the worktree-add process.")
            time.sleep(0.01)
        process.send_signal(signal.SIGUSR1)
        _stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 23, stderr
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        if marker.is_file():
            git_pid = int(marker.read_text(encoding="utf-8").strip())
            with suppress(ProcessLookupError):
                os.killpg(git_pid, signal.SIGKILL)

    manager = _manager(caller, remote)
    assert _caller_state(caller) == before
    assert manager.state_dir.is_dir()
    assert not manager.worktree_path.exists()
    assert not manager.receipt_path.exists()


def test_sigkilled_launch_intake_sweep_recovers_an_abandoned_pointer(
    tmp_path: Path,
) -> None:
    """A SIGKILLed launch's intake is recovered by the next launch's sweep.

    Process-level coverage spawns a real helper subprocess. The first
    launch prepares its intake and holds no lock owner while it idles;
    SIGKILL reproduces the host-failure state exactly. The kernel releases
    every lock owner, and the second launch's abandoned-intake sweep then
    proves and removes the leftover intake before creating its own.
    """
    pytest.importorskip("fcntl")
    source_root = Path(__file__).resolve().parents[3]
    caller, remote = _make_repository(tmp_path)
    marker = tmp_path / "first.intake.json"
    first_ready = tmp_path / "first.ready"
    before = _caller_state(caller)
    child = r"""
import json
import subprocess
import sys
import uuid
from pathlib import Path

from hephaestus.automation.repo_intake import RepoIntakeManager
from hephaestus.git.snapshot import _controlled_git_env

caller = Path(sys.argv[1])
remote = Path(sys.argv[2])
marker = Path(sys.argv[3])
ready = Path(sys.argv[4])

def runner(command, *, cwd=None, check=True, timeout=None, env=None,
           log_errors=True, input_text=None):
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
        input=input_text,
    )

manager = RepoIntakeManager(
    caller,
    repository="acme/repo",
    gh_command="gh",
    timeout_s=30,
    git_runner=runner,
    git_env=_controlled_git_env(),
    remote_config=(),
    launch_identity=uuid.uuid4().hex,
)
receipt = manager.prepare()
marker.write_text(json.dumps(receipt.to_dict()), encoding="utf-8")
ready.write_text("prepared\n", encoding="utf-8")
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(source_root)
    child_process = subprocess.Popen(
        [sys.executable, "-c", child, str(caller), str(remote), str(marker), str(first_ready)],
        cwd=source_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 30.0
        while not first_ready.is_file():
            if child_process.poll() is not None:
                _stdout, stderr = child_process.communicate()
                pytest.fail(f"The first launch exited before its marker: {stderr}")
            if time.monotonic() >= deadline:
                pytest.fail("The first launch did not finish its intake preparation.")
            time.sleep(0.01)
        abandoned_receipt = json.loads(marker.read_text(encoding="utf-8"))
        os.killpg(child_process.pid, signal.SIGKILL)
        child_process.wait(timeout=10)
    finally:
        if child_process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(child_process.pid, signal.SIGKILL)
            child_process.wait(timeout=5)

    abandoned_worktree = Path(abandoned_receipt["path"])
    abandoned_launch_root = Path(abandoned_receipt["state_root"])
    assert abandoned_worktree.is_dir()
    assert abandoned_launch_root.is_dir()

    manager = _manager(caller, remote, "recovery-launch")
    recovered_receipt = manager.prepare()

    assert recovered_receipt.revision == abandoned_receipt["revision"]
    assert recovered_receipt.path != abandoned_worktree
    assert not abandoned_launch_root.exists()
    remaining = _run_git(caller, "worktree", "list", "--porcelain").stdout.split("worktree ")[1:]
    assert not any(entry.startswith(str(abandoned_worktree)) for entry in remaining)
    assert _caller_state(caller) == before


@pytest.mark.parametrize("stop", ["deadline", "cancellation"])
def test_intake_metadata_lock_observes_operation_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    """Metadata contention stops before intake preparation can change state."""
    fcntl = pytest.importorskip("fcntl")
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    metadata_lock = WorktreeManager.git_metadata_lock_path(caller)
    with file_lock(metadata_lock, require_exclusive=True):
        metadata_inode = metadata_lock.stat().st_ino
        observed = threading.Event()
        complete = threading.Event()
        shutdown = threading.Event()
        clock = [time.monotonic()]
        deadline = clock[0] + 60.0
        failures: list[BaseException] = []
        effects: list[str] = []
        real_flock = fcntl.flock

        monkeypatch.setattr(
            git_runtime,
            "time",
            SimpleNamespace(monotonic=lambda: clock[0], sleep=time.sleep),
        )
        monkeypatch.setattr(
            manager,
            "_prepare_locked",
            lambda: effects.append("prepared"),
        )

        def observe_flock(fd: int, flags: int) -> None:
            if (
                threading.current_thread() is worker
                and flags & fcntl.LOCK_EX
                and os.fstat(fd).st_ino == metadata_inode
            ):
                if stop == "deadline":
                    clock[0] = deadline + 1.0
                else:
                    shutdown.set()
                observed.set()
            real_flock(fd, flags)

        def prepare() -> None:
            try:
                with git_runtime.operation_deadline(
                    deadline,
                    shutdown=shutdown if stop == "cancellation" else None,
                ):
                    manager.prepare()
            except BaseException as exc:
                failures.append(exc)
            finally:
                complete.set()

        monkeypatch.setattr(fcntl, "flock", observe_flock)
        worker = threading.Thread(target=prepare, daemon=True)
        worker.start()
        assert observed.wait(5.0)
        stopped_before_release = complete.wait(1.0)

    worker.join(timeout=5.0)
    assert not worker.is_alive()
    assert stopped_before_release, "Intake waited beyond its operation stop."
    assert effects == []
    assert len(failures) == 1
    expected = subprocess.TimeoutExpired if stop == "deadline" else InterruptedError
    assert isinstance(failures[0], expected)
    assert not manager.receipt_path.exists()
    assert not manager.worktree_path.exists()
    assert not manager.registration_path.exists()


def test_intake_rejects_unavailable_exclusive_lock_without_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host without exclusive locks fails before intake work starts."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    real_import = builtins.__import__
    effects: list[str] = []

    def import_without_fcntl(name: str, *args: object, **kwargs: object) -> object:
        if name == "fcntl":
            raise ImportError("injected host without exclusive file locks")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(builtins, "__import__", import_without_fcntl)
    monkeypatch.setattr(manager, "_prepare_locked", lambda: effects.append("prepared"))

    with pytest.raises(
        RepoIntakeError, match="exclusive repository-intake launch locking is unavailable"
    ):
        manager.prepare()

    assert effects == []
    assert not manager.receipt_path.exists()
    assert not manager.worktree_path.exists()


@pytest.mark.parametrize("failure", ["missing-module", "unsupported-operation"])
def test_owner_lock_reports_unavailable_exclusive_lock_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A permanent owner-lock failure is not reported as active contention."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    if failure == "missing-module":
        real_import = builtins.__import__

        def import_without_fcntl(name: str, *args: object, **kwargs: object) -> object:
            if name == "fcntl":
                raise ImportError("injected host without exclusive file locks")
            return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(builtins, "__import__", import_without_fcntl)
    else:
        fcntl = pytest.importorskip("fcntl")

        def unsupported_flock(*_args: object) -> None:
            raise OSError(errno.ENOTSUP, "injected unsupported file lock")

        monkeypatch.setattr(fcntl, "flock", unsupported_flock)

    with pytest.raises(
        RepoIntakeError,
        match="exclusive repository-intake launch locking is unavailable",
    ):
        with manager.owner():
            pytest.fail("Unavailable exclusive locking admitted the owner lock.")


def test_owner_lock_blocks_a_second_process_before_preparation(tmp_path: Path) -> None:
    """A live launch keeps a second process from preparing the same intake."""
    source_root = Path(__file__).resolve().parents[3]
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    first = manager.prepare()
    _advance_remote(tmp_path, remote)
    child = """
import sys
from pathlib import Path
from hephaestus.automation import git_utils
from hephaestus.automation.repo_intake import RepoIntakeManager
from hephaestus.git.snapshot import _controlled_git_env

manager = RepoIntakeManager(
    Path(sys.argv[1]),
    repository="acme/repo",
    gh_command="gh",
    timeout_s=30,
    git_runner=git_utils.run,
    git_env=_controlled_git_env(),
    remote_config=(),
    launch_identity=sys.argv[2],
)
try:
    with manager.owner():
        raise SystemExit(0)
except Exception as error:
    print(f"{type(error).__name__}: {error}", file=sys.stderr)
    raise SystemExit(23)
"""

    with manager.owner():
        attempted = subprocess.run(
            [sys.executable, "-c", child, str(caller), manager.launch_identity],
            cwd=source_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )

    assert attempted.returncode == 23
    assert "RepoIntakeError: repository_intake_launch_in_use:" in attempted.stderr
    assert "use a unique launch identity and retry" in attempted.stderr
    assert _run_git(first.path, "rev-parse", "HEAD").stdout.strip() == first.revision


def test_owner_lock_uses_a_stable_launch_directory_path(tmp_path: Path) -> None:
    """One launch identity selects one owner lock for its launch directory."""
    caller, remote = _make_repository(tmp_path)
    linked = tmp_path / "lease-linked-caller"
    _run_git(caller, "worktree", "add", "--detach", str(linked), "HEAD")
    primary = _manager(caller, remote, "shared-launch")
    secondary = _manager(linked, remote, "shared-launch")

    assert primary.owner_lock_path == secondary.owner_lock_path
    with primary.owner():
        with pytest.raises(RepoIntakeError) as caught:
            with secondary.owner():
                pass
    assert "repository_intake_launch_in_use" in str(caught.value)


def test_intake_launch_in_use_error_stays_exported_for_callers() -> None:
    """The legacy in-use error remains an importable intake failure."""
    from hephaestus.automation.repo_intake import RepoIntakeInUseError

    assert issubclass(RepoIntakeInUseError, RepoIntakeError)


def test_intake_lock_interruption_preserves_shutdown_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted metadata wait is not converted to an intake failure."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)

    class _InterruptedLock:
        def __enter__(self) -> None:
            raise InterruptedError("stop")

        def __exit__(self, *args: object) -> None:
            del args

    monkeypatch.setattr(
        "hephaestus.automation.repo_intake.operation_file_lock",
        lambda *_args, **_kwargs: _InterruptedLock(),
    )

    with pytest.raises(InterruptedError, match="stop"):
        manager.prepare()


def test_a_dirty_managed_source_lane_does_not_block_intake(tmp_path: Path) -> None:
    """A dirty managed source lane is the automation's own work, not a fault."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.state_parent.mkdir(mode=0o700, exist_ok=True)
    manager.state_dir.mkdir(mode=0o700, exist_ok=True)
    lane = manager.state_dir / "build" / ".worktrees" / "auto-7-impl"
    lane.parent.mkdir(mode=0o700, parents=True)
    _run_git(caller, "worktree", "add", "--detach", str(lane), "HEAD")
    (lane / "tracked.txt").write_text("writer work\n", encoding="utf-8")

    receipt = manager.prepare()

    assert receipt.path.exists()
    assert (lane / "tracked.txt").read_text(encoding="utf-8") == "writer work\n"


def test_a_dirty_source_lane_beside_the_caller_does_not_block_intake(tmp_path: Path) -> None:
    """A dirty source lane under the caller worker base is the automation's own work."""
    caller, remote = _make_repository(tmp_path)
    lane = caller / "build" / ".worktrees" / "auto-7-impl"
    lane.parent.mkdir(parents=True, exist_ok=True)
    _run_git(caller, "worktree", "add", "--detach", str(lane), "HEAD")
    (lane / "tracked.txt").write_text("writer work\n", encoding="utf-8")
    manager = _manager(caller, remote)

    receipt = manager.prepare()

    assert receipt.path.exists()
    assert (lane / "tracked.txt").read_text(encoding="utf-8") == "writer work\n"


def test_prepare_accepts_a_group_writable_common_directory(tmp_path: Path) -> None:
    """A group-writable Git common directory is host policy, not intake state.

    The host umask sets this mode. The intake check does not read it.
    """
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.common_dir.chmod(0o775)

    receipt = manager.prepare()

    assert receipt.common_dir == manager.common_dir


def test_prepare_accepts_a_group_readable_common_directory(tmp_path: Path) -> None:
    """A group-readable Git common directory passes the permission preflight."""
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    manager.common_dir.chmod(0o755)

    receipt = manager.prepare()

    assert receipt.common_dir == manager.common_dir


def test_prepare_accepts_a_common_directory_owned_by_another_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A foreign-owned common directory does not block intake.

    The loop runs in a caller-owned checkout. Ownership is not its policy.
    """
    caller, remote = _make_repository(tmp_path)
    manager = _manager(caller, remote)
    monkeypatch.setattr("hephaestus.automation.repo_intake.os.geteuid", lambda: 0)

    receipt = manager.prepare()

    assert receipt.common_dir == manager.common_dir
