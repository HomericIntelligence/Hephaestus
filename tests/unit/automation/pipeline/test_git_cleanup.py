"""Tests for terminal Git and Codex session-root cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline import git_cleanup
from hephaestus.automation.pipeline.git_cleanup import run_cleanup_job
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.worktree_manager import WorktreeManager


def _remove_job(repo_root: Path, worktree: Path) -> GitJob:
    """Return one receipt-bound terminal cleanup request."""
    return GitJob(
        repo="test/repo",
        op="remove_worktree",
        timeout_s=60,
        kwargs={
            "worktree_path": str(worktree),
            "repo_root": str(repo_root),
            "issue_number": 7,
            "expected_head": "a" * 40,
            "expected_detached": True,
            "force": False,
        },
    )


def _session_root(worktree: Path) -> Path:
    """Return the sibling session root for the test worktree."""
    return worktree.parent / f".{worktree.name}-codex-sessions"


def _make_session_root(worktree: Path) -> Path:
    """Create a session root with sealed profile and authentication residue."""
    root = _session_root(worktree)
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    identity = root / ("b" * 64)
    profile = identity / ".runs" / ("c" * 64) / "profile"
    profile.mkdir(parents=True, mode=0o700)
    (profile / "config.toml").write_text("model = 'test'\n", encoding="utf-8")
    (profile / "config.toml").chmod(0o400)
    profile.chmod(0o500)
    sessions = identity / "sessions"
    sessions.mkdir(mode=0o700)
    (sessions / "rollout.jsonl").write_text("{}\n", encoding="utf-8")
    authentication = identity / ".runs" / ("c" * 64) / ".transient-auth" / "run-one"
    authentication.mkdir(parents=True, mode=0o700)
    (authentication / "auth.json").write_text("{}", encoding="utf-8")
    return root


def _profile_path(worktree: Path) -> Path:
    """Return the test request's private profile path."""
    return _session_root(worktree) / ("b" * 64) / ".runs" / ("c" * 64) / "profile"


def _registered_record(worktree: Path) -> list[dict[str, str]]:
    return [{"path": str(worktree), "commit": "a" * 40}]


def _quarantine_payload(
    worktree: Path,
    *,
    repository: str = "test/repo",
) -> dict[str, object]:
    """Return one terminal-state quarantine payload for the test profile."""
    identity = "b" * 64
    run_nonce = "c" * 64
    return {
        "issue": 7,
        "private_profile_path": str(
            _session_root(worktree) / identity / ".runs" / run_nonce / "profile"
        ),
        "repository": repository,
        "run_nonce": run_nonce,
        "session_identity_digest": "d" * 64,
        "status": "terminal-state-invalid",
        "worktree_path": str(worktree),
    }


def _write_quarantine(worktree: Path, payload: dict[str, object] | None = None) -> Path:
    """Write one canonical owner-only quarantine receipt."""
    receipt = _session_root(worktree) / ("b" * 64) / ".quarantine.json"
    receipt.write_text(
        json.dumps(payload or _quarantine_payload(worktree), separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    receipt.chmod(0o400)
    return receipt


def test_remove_worktree_deletes_codex_session_root_before_git(tmp_path: Path) -> None:
    """Terminal cleanup removes sealed Codex state before the checkout."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[:3] == ["git", "worktree", "remove"]:
            assert not session_root.exists()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run) as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert not session_root.exists()
    assert any(call.args[0][:3] == ["git", "worktree", "remove"] for call in git_run.call_args_list)


def test_remove_worktree_accepts_terminal_quarantine_before_git(tmp_path: Path) -> None:
    """A bound terminal quarantine permits deletion before Git cleanup."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[:3] == ["git", "worktree", "remove"]:
            assert not session_root.exists()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run) as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert not session_root.exists()
    assert any(call.args[0][:3] == ["git", "worktree", "remove"] for call in git_run.call_args_list)


def test_remove_worktree_quarantine_forces_dirty_terminal_checkout_removal(
    tmp_path: Path,
) -> None:
    """A bound quarantine permits forced removal of contaminated state."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    secret = worktree / "late-credential.txt"
    secret.write_text("test-only credential residue", encoding="utf-8")
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[:2] == ["git", "status"]:
            assert session_root.exists()
            return SimpleNamespace(stdout="?? late-credential.txt\n")
        if command[:3] == ["git", "worktree", "remove"]:
            assert command == ["git", "worktree", "remove", "--force", str(worktree)]
            assert not session_root.exists()
            assert tombstone.exists()
            secret.unlink()
            worktree.rmdir()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run),
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert not worktree.exists()
    assert not secret.exists()
    assert not tombstone.exists()


def test_remove_worktree_retry_retains_dirty_quarantine_authority(tmp_path: Path) -> None:
    """A failed forced removal retains its exact authority for one retry."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    secret = worktree / "late-credential.txt"
    secret.write_text("test-only credential residue", encoding="utf-8")
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    forced_removals = 0

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal forced_removals
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="?? late-credential.txt\n")
        if command[:3] == ["git", "worktree", "remove"]:
            forced_removals += 1
            assert command == ["git", "worktree", "remove", "--force", str(worktree)]
            assert not session_root.exists()
            assert tombstone.exists()
            if forced_removals == 1:
                raise OSError("temporary Git failure")
            secret.unlink()
            worktree.rmdir()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run),
    ):
        with pytest.raises(OSError, match="temporary Git failure"):
            run_cleanup_job(_remove_job(tmp_path, worktree))
        assert tombstone.exists()
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert forced_removals == 2
    assert not worktree.exists()
    assert not secret.exists()
    assert not tombstone.exists()


def test_remove_worktree_deletes_deep_quarantined_tree_without_recursion(
    tmp_path: Path,
) -> None:
    """Terminal cleanup deletes a tree deeper than Python recursion permits."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    runs = session_root / ("b" * 64) / ".runs"
    directory_descriptor = os.open(runs, os.O_RDONLY | os.O_DIRECTORY)
    secret_descriptor = -1
    try:
        for _depth in range(1_205):
            os.mkdir("deep", mode=0o700, dir_fd=directory_descriptor)
            child = os.open(
                "deep",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = child
        secret_descriptor = os.open(
            "late-credential.txt",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_descriptor,
        )
        os.write(secret_descriptor, b"test-only credential residue")

        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            result = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert result.ok is True
        assert not session_root.exists()
        assert not tombstone.exists()
        assert os.fstat(secret_descriptor).st_nlink == 0
    finally:
        if secret_descriptor >= 0:
            os.close(secret_descriptor)
        os.close(directory_descriptor)


def test_remove_worktree_resumes_interrupted_iterative_quarantine_cleanup(
    tmp_path: Path,
) -> None:
    """A real cleanup retry reuses the bound receipt after flatten fails."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    secret = worktree / "late-credential.txt"
    secret.write_text("test-only credential residue", encoding="utf-8")
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    identity = session_root / ("b" * 64)
    staged_receipt = (
        tombstone
        / git_cleanup._CODEX_DELETE_STAGING
        / git_cleanup._codex_delete_identity_name("b" * 64, identity.stat())
        / ".quarantine.json"
    )
    original_open = git_cleanup._open_owned_codex_directory
    failed = False
    forced_removals = 0

    def fail_one_staged_directory(
        parent_descriptor: int,
        name: str,
        expected: os.stat_result,
    ) -> tuple[int, os.stat_result]:
        nonlocal failed
        if name.startswith("node-") and not failed:
            failed = True
            raise OSError("injected iterative cleanup failure")
        return original_open(parent_descriptor, name, expected)

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        nonlocal forced_removals
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="?? late-credential.txt\n")
        if command[:3] == ["git", "worktree", "remove"]:
            forced_removals += 1
            assert command == ["git", "worktree", "remove", "--force", str(worktree)]
            if forced_removals == 2:
                secret.unlink()
                worktree.rmdir()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch.object(
            git_cleanup,
            "_open_owned_codex_directory",
            side_effect=fail_one_staged_directory,
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run),
    ):
        first = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert first.ok is False
        assert first.error == "Codex session root cleanup failed"
        assert staged_receipt.is_file()
        assert not session_root.exists()
        assert tombstone.exists()
        assert secret.exists()

        second = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert second.ok is True
    assert forced_removals == 2
    assert not worktree.exists()
    assert not secret.exists()
    assert not staged_receipt.exists()
    assert not tombstone.exists()


def test_remove_worktree_retry_preserves_swapped_staged_directory(tmp_path: Path) -> None:
    """A retry rejects a staged name that does not bind its encoded inode."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    profile = _profile_path(worktree)
    profile.chmod(0o700)
    guest = profile / "guest"
    guest.mkdir()
    (guest / "original.txt").write_text("original", encoding="utf-8")
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    original_open = git_cleanup._open_owned_codex_directory
    swapped = False
    replacement_descriptor = -1

    def swap_before_rename(
        parent_descriptor: int,
        name: str,
        expected: os.stat_result,
    ) -> tuple[int, os.stat_result]:
        nonlocal replacement_descriptor, swapped
        opened = original_open(parent_descriptor, name, expected)
        if name == "guest" and not swapped:
            swapped = True
            os.rename(
                name,
                "escaped-original",
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            replacement_directory = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            try:
                replacement_descriptor = os.open(
                    "replacement-secret.txt",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=replacement_directory,
                )
                os.write(replacement_descriptor, b"replacement must remain")
            finally:
                os.close(replacement_directory)
        return opened

    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch.object(
                git_cleanup,
                "_open_owned_codex_directory",
                side_effect=swap_before_rename,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            first = run_cleanup_job(_remove_job(tmp_path, worktree))
            second = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert first.ok is False
        assert second.ok is False
        assert replacement_descriptor >= 0
        assert os.fstat(replacement_descriptor).st_nlink == 1
        assert tombstone.exists()
        assert (
            sum(
                bool(call.args and call.args[0][:3] == ["git", "worktree", "remove"])
                for call in git_run.call_args_list
            )
            == 1
        )
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)


def test_remove_worktree_retry_preserves_swapped_staged_identity(tmp_path: Path) -> None:
    """A retry rejects an identity holder that does not bind its encoded inode."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    identity = "b" * 64
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    original_open = git_cleanup._open_owned_codex_directory
    swapped = False
    replacement_descriptor = -1

    def swap_before_rename(
        parent_descriptor: int,
        name: str,
        expected: os.stat_result,
    ) -> tuple[int, os.stat_result]:
        nonlocal replacement_descriptor, swapped
        opened = original_open(parent_descriptor, name, expected)
        if name == identity and not swapped:
            swapped = True
            os.rename(
                name,
                "escaped-original",
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            replacement_directory = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_descriptor,
            )
            try:
                replacement_descriptor = os.open(
                    "replacement-secret.txt",
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=replacement_directory,
                )
                os.write(replacement_descriptor, b"replacement must remain")
            finally:
                os.close(replacement_directory)
        return opened

    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch.object(
                git_cleanup,
                "_open_owned_codex_directory",
                side_effect=swap_before_rename,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            first = run_cleanup_job(_remove_job(tmp_path, worktree))
            second = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert first.ok is False
        assert second.ok is False
        assert replacement_descriptor >= 0
        assert os.fstat(replacement_descriptor).st_nlink == 1
        assert tombstone.exists()
        assert (
            sum(
                bool(call.args and call.args[0][:3] == ["git", "worktree", "remove"])
                for call in git_run.call_args_list
            )
            == 1
        )
    finally:
        if replacement_descriptor >= 0:
            os.close(replacement_descriptor)


@pytest.mark.parametrize("nested_name", [".active.json", ".quarantine.json"])
def test_remove_worktree_unlinks_nested_control_basename(
    tmp_path: Path,
    nested_name: str,
) -> None:
    """A control basename is ordinary guest data below the identity store."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    _profile_path(worktree).chmod(0o700)
    nested = _profile_path(worktree) / "guest" / nested_name
    nested.parent.mkdir()
    nested.write_text("guest data", encoding="utf-8")
    descriptor = os.open(nested, os.O_RDONLY)
    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            result = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert result.ok is True
        assert os.fstat(descriptor).st_nlink == 0
        assert not session_root.with_name(f"{session_root.name}.terminal-cleanup").exists()
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("leaf_kind", ["fifo", "symlink"])
def test_remove_worktree_unlinks_nested_owned_special_leaf(
    tmp_path: Path,
    leaf_kind: str,
) -> None:
    """Terminal cleanup unlinks owned guest leaves without following them."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    _profile_path(worktree).chmod(0o700)
    guest = _profile_path(worktree) / "guest"
    guest.mkdir()
    credential = guest / "late-credential.txt"
    credential.write_text("test-only credential residue", encoding="utf-8")
    credential_descriptor = os.open(credential, os.O_RDONLY)
    leaf = guest / "special-leaf"
    target = tmp_path / "outside-target"
    target.write_text("keep", encoding="utf-8")
    if leaf_kind == "fifo":
        os.mkfifo(leaf, mode=0o600)
    else:
        leaf.symlink_to(target)
    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            result = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert result.ok is True
        assert os.fstat(credential_descriptor).st_nlink == 0
        assert target.read_text(encoding="utf-8") == "keep"
        assert not session_root.with_name(f"{session_root.name}.terminal-cleanup").exists()
    finally:
        os.close(credential_descriptor)


def test_remove_worktree_unlinks_nested_mode_zero_entries(tmp_path: Path) -> None:
    """Terminal cleanup normalizes an owned sealed directory before deletion."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    _profile_path(worktree).chmod(0o700)
    sealed = _profile_path(worktree) / "sealed"
    sealed.mkdir()
    credential = sealed / "late-credential.txt"
    credential.write_text("test-only credential residue", encoding="utf-8")
    descriptor = os.open(credential, os.O_RDONLY)
    credential.chmod(0o000)
    sealed.chmod(0o000)
    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            result = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert result.ok is True
        assert os.fstat(descriptor).st_nlink == 0
        assert not session_root.with_name(f"{session_root.name}.terminal-cleanup").exists()
    finally:
        os.close(descriptor)


def test_remove_worktree_entry_cap_makes_progress_across_retries(tmp_path: Path) -> None:
    """Each bounded cleanup retry removes more staged guest state."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    profile = _profile_path(worktree)
    profile.chmod(0o700)
    secret = profile / "guest-11" / "late-credential.txt"
    for number in range(12):
        guest = profile / f"guest-{number}"
        guest.mkdir()
        (guest / "data.txt").write_text("guest data", encoding="utf-8")
    secret.write_text("test-only credential residue", encoding="utf-8")
    secret_descriptor = os.open(secret, os.O_RDONLY)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    try:
        results = []
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch.object(git_cleanup, "_CODEX_SESSION_CLEANUP_MAX_ENTRIES", 8),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            for _attempt in range(20):
                result = run_cleanup_job(_remove_job(tmp_path, worktree))
                results.append(result)
                if result.ok:
                    break

        assert results[-1].ok is True
        assert any(result.ok is False for result in results[:-1])
        assert all(
            isinstance(result.value, dict) and result.value.get("cleanup_progress") is True
            for result in results[:-1]
        )
        assert os.fstat(secret_descriptor).st_nlink == 0
        assert not tombstone.exists()
    finally:
        os.close(secret_descriptor)


def test_remove_worktree_unlinks_leaf_larger_than_byte_cap(tmp_path: Path) -> None:
    """Logical file size does not prevent descriptor-relative unlink."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    profile = _profile_path(worktree)
    profile.chmod(0o700)
    oversized = profile / "oversized-guest-data"
    oversized.write_bytes(b"x" * 32)
    descriptor = os.open(oversized, os.O_RDONLY)
    try:
        with (
            patch.object(
                WorktreeManager,
                "list_worktrees",
                return_value=_registered_record(worktree),
            ),
            patch.object(git_cleanup, "_CODEX_SESSION_CLEANUP_MAX_BYTES", 8),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
        ):
            git_run.return_value.stdout = ""
            result = run_cleanup_job(_remove_job(tmp_path, worktree))

        assert result.ok is True
        assert os.fstat(descriptor).st_nlink == 0
        assert not session_root.with_name(f"{session_root.name}.terminal-cleanup").exists()
    finally:
        os.close(descriptor)


def test_remove_worktree_without_quarantine_refuses_dirty_checkout(tmp_path: Path) -> None:
    """A dirty ordinary worktree remains preserved without terminal proof."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    secret = worktree / "late-credential.txt"
    secret.write_text("test-only credential residue", encoding="utf-8")

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[:2] == ["git", "status"]:
            return SimpleNamespace(stdout="?? late-credential.txt\n")
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run) as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "worktree cleanup refused a dirty checkout"
    assert secret.exists()
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_accepts_owner_suffix_for_local_repo_key(tmp_path: Path) -> None:
    """A local repository key accepts one bound owner/repository receipt."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    _make_session_root(worktree)
    _write_quarantine(worktree, _quarantine_payload(worktree, repository="owner/repo"))
    job = GitJob(
        repo="repo",
        op="remove_worktree",
        timeout_s=60,
        kwargs=dict(_remove_job(tmp_path, worktree).kwargs),
    )

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(job)

    assert result.ok is True


@pytest.mark.parametrize(
    "receipt_kind",
    ["hard_link", "malformed", "noncanonical", "symlink", "wrong_mode"],
)
def test_remove_worktree_rejects_invalid_terminal_quarantine(
    tmp_path: Path,
    receipt_kind: str,
) -> None:
    """Malformed, linked, or writable quarantine receipts block cleanup."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    receipt = session_root / ("b" * 64) / ".quarantine.json"
    if receipt_kind == "symlink":
        outside = tmp_path / "outside.json"
        outside.write_text("{}", encoding="utf-8")
        receipt.symlink_to(outside)
    else:
        receipt = _write_quarantine(worktree)
        if receipt_kind in {"malformed", "noncanonical"}:
            receipt.chmod(0o600)
            receipt.write_text(
                "{}"
                if receipt_kind == "malformed"
                else json.dumps(_quarantine_payload(worktree), sort_keys=True),
                encoding="utf-8",
            )
            receipt.chmod(0o400)
        elif receipt_kind == "hard_link":
            os.link(receipt, tmp_path / "quarantine-link.json")
        else:
            receipt.chmod(0o600)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session quarantine receipt is invalid"
    assert session_root.exists()
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issue", 8),
        ("repository", "other/repo"),
        ("worktree_path", "/tmp/other/issue-7"),
        ("private_profile_path", "/tmp/other/profile"),
        ("run_nonce", "e" * 64),
        ("session_identity_digest", "D" * 64),
        ("status", "active"),
    ],
)
def test_remove_worktree_rejects_mismatched_terminal_quarantine(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    """A quarantine receipt must bind to the exact cleanup request."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    payload = _quarantine_payload(worktree)
    payload[field] = value
    _write_quarantine(worktree, payload)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session quarantine receipt is invalid"
    assert session_root.exists()


def test_remove_worktree_revalidates_quarantine_before_unlink(tmp_path: Path) -> None:
    """A replaced quarantine cannot retain terminal cleanup authority."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    _write_quarantine(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    original_remove = git_cleanup._remove_codex_session_tree_contents
    root_inode = session_root.stat().st_ino
    replaced = False

    def replace_after_validation(descriptor: int, **kwargs: object) -> None:
        nonlocal replaced
        if os.fstat(descriptor).st_ino == root_inode and not replaced:
            replaced = True
            receipt = tombstone / ("b" * 64) / ".quarantine.json"
            receipt.chmod(0o600)
            receipt.write_text("{}", encoding="utf-8")
            receipt.chmod(0o400)
        original_remove(descriptor, **cast(Any, kwargs))

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch.object(
            git_cleanup,
            "_remove_codex_session_tree_contents",
            side_effect=replace_after_validation,
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session quarantine receipt is invalid"
    assert tombstone.exists()
    assert (
        sum(
            bool(call.args and call.args[0][:3] == ["git", "worktree", "remove"])
            for call in git_run.call_args_list
        )
        == 1
    )


def test_remove_worktree_cleans_orphan_session_root_after_restart(tmp_path: Path) -> None:
    """A restart can reap session state after Git already removed the checkout."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    session_root = _make_session_root(worktree)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=[]),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert not session_root.exists()
    git_run.assert_not_called()


def test_remove_worktree_cleans_detached_session_tombstone_after_restart(tmp_path: Path) -> None:
    """A restart completes cleanup after the session root was detached."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    session_root = _make_session_root(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    session_root.rename(tombstone)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=[]),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is True
    assert not tombstone.exists()
    git_run.assert_not_called()


@pytest.mark.parametrize(
    "receipt_path",
    [
        Path("b" * 64) / ".active.json",
        Path("b" * 64) / ".invalid.active.json",
        Path(".quarantine.json"),
    ],
)
def test_remove_worktree_preserves_session_root_with_host_control_receipt(
    tmp_path: Path,
    receipt_path: Path,
) -> None:
    """An active or malformed control receipt blocks terminal deletion."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    (session_root / receipt_path).write_text("{}", encoding="utf-8")

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root contains an active or invalid control receipt"
    assert session_root.exists()
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_rejects_symlinked_session_root(tmp_path: Path) -> None:
    """Terminal cleanup never follows a replacement session-root link."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep"
    marker.write_text("keep", encoding="utf-8")
    _session_root(worktree).symlink_to(outside, target_is_directory=True)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root identity is invalid"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_quarantines_receipt_created_during_detach(tmp_path: Path) -> None:
    """A receipt that appears during cleanup is retained and blocks Git removal."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    original_validate = git_cleanup._validate_codex_session_root_children
    calls = 0

    def validate(descriptor: int, **kwargs: object) -> bool:
        nonlocal calls
        has_quarantine = original_validate(descriptor, **cast(Any, kwargs))
        calls += 1
        if calls == 2:
            (session_root / ("b" * 64) / ".active.json").write_text("{}", encoding="utf-8")
        return has_quarantine

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch.object(git_cleanup, "_validate_codex_session_root_children", side_effect=validate),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root contains an active or invalid control receipt"
    assert not session_root.exists()
    assert (tombstone / ("b" * 64) / ".active.json").is_file()
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_reports_session_root_deletion_failure(tmp_path: Path) -> None:
    """A filesystem deletion failure blocks Git and returns cleanup evidence."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    tombstone_name = f"{session_root.name}.terminal-cleanup"
    original_rmdir = os.rmdir

    def fail_terminal_rmdir(name: str, *, dir_fd: int | None = None) -> None:
        if name == tombstone_name:
            raise OSError
        original_rmdir(name, dir_fd=dir_fd)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch(
            "hephaestus.automation.pipeline.git_cleanup.os.rmdir", side_effect=fail_terminal_rmdir
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root cleanup failed"
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_does_not_delete_replacement_tombstone(tmp_path: Path) -> None:
    """A same-user replacement cannot redirect terminal recursive deletion."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    escaped = session_root.with_name(f"{session_root.name}.attacker-renamed")
    victim = tombstone / "victim.txt"
    original_remove = git_cleanup._remove_codex_session_tree_contents
    root_inode = session_root.stat().st_ino
    raced = False

    def replace_after_validation(descriptor: int, **kwargs: object) -> None:
        nonlocal raced
        if os.fstat(descriptor).st_ino == root_inode and not raced:
            raced = True
            tombstone.rename(escaped)
            tombstone.mkdir(mode=0o700)
            victim.write_text("keep", encoding="utf-8")
        original_remove(descriptor, **cast(Any, kwargs))

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch.object(
            git_cleanup,
            "_remove_codex_session_tree_contents",
            side_effect=replace_after_validation,
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert victim.read_text(encoding="utf-8") == "keep"
    assert escaped.exists()
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


def test_remove_worktree_cleans_validated_inode_after_tombstone_rename(tmp_path: Path) -> None:
    """Cleanup stays on the opened inode when its terminal name is removed."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    tombstone = session_root.with_name(f"{session_root.name}.terminal-cleanup")
    escaped = session_root.with_name(f"{session_root.name}.attacker-renamed")
    original_remove = git_cleanup._remove_codex_session_tree_contents
    root_inode = session_root.stat().st_ino
    raced = False

    def rename_after_validation(descriptor: int, **kwargs: object) -> None:
        nonlocal raced
        if os.fstat(descriptor).st_ino == root_inode and not raced:
            raced = True
            tombstone.rename(escaped)
        original_remove(descriptor, **cast(Any, kwargs))

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch.object(
            git_cleanup,
            "_remove_codex_session_tree_contents",
            side_effect=rename_after_validation,
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert escaped.is_dir()
    assert list(escaped.iterdir()) == []
    assert not any(
        call.args and call.args[0][:3] == ["git", "worktree", "remove"]
        for call in git_run.call_args_list
    )


@pytest.mark.parametrize("invalid_mode", [0o755, 0o500])
def test_remove_worktree_rejects_session_root_without_owner_only_mode(
    tmp_path: Path,
    invalid_mode: int,
) -> None:
    """Terminal cleanup accepts only the fixed owner-only root mode."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)
    session_root.chmod(invalid_mode)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root identity is invalid"
    assert session_root.exists()


def test_remove_worktree_rejects_session_root_owned_by_another_user(tmp_path: Path) -> None:
    """Terminal cleanup refuses a session root outside the effective user."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    session_root = _make_session_root(worktree)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=_registered_record(worktree)),
        patch(
            "hephaestus.automation.pipeline.git_cleanup.os.geteuid", return_value=os.geteuid() + 1
        ),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        git_run.return_value.stdout = ""
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "Codex session root identity is invalid"
    assert session_root.exists()


def test_remove_worktree_rejects_dangling_valid_path_symlink(tmp_path: Path) -> None:
    """A dangling worktree link cannot redirect sibling session cleanup."""
    (tmp_path / ".git").mkdir()
    linked_target = tmp_path / "build" / ".worktrees" / "issue-7"
    linked_target.parent.mkdir(parents=True)
    worktree = tmp_path / "issue-7"
    worktree.symlink_to(linked_target, target_is_directory=True)
    redirected_root = _make_session_root(linked_target)

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=[]),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "worktree cleanup identity is invalid"
    assert redirected_root.exists()
    git_run.assert_not_called()


def test_remove_worktree_rejects_rebound_valid_path_symlink(tmp_path: Path) -> None:
    """A rebound worktree link cannot adopt another registered checkout."""
    (tmp_path / ".git").mkdir()
    linked_target = tmp_path / "build" / ".worktrees" / "issue-7"
    linked_target.mkdir(parents=True)
    worktree = tmp_path / "issue-7"
    worktree.symlink_to(linked_target, target_is_directory=True)
    redirected_root = _make_session_root(linked_target)
    records = [{"path": str(linked_target), "commit": "a" * 40}]

    with (
        patch.object(WorktreeManager, "list_worktrees", return_value=records),
        patch("hephaestus.automation.pipeline.git_cleanup.run") as git_run,
    ):
        result = run_cleanup_job(_remove_job(tmp_path, worktree))

    assert result.ok is False
    assert result.error == "worktree cleanup identity is invalid"
    assert redirected_root.exists()
    git_run.assert_not_called()


def test_remove_worktree_retry_releases_branch_after_checkout_is_absent(
    tmp_path: Path,
) -> None:
    """A retry completes branch cleanup after Git removed the checkout."""
    (tmp_path / ".git").mkdir()
    worktree = tmp_path / "issue-7"
    worktree.mkdir()
    job = _remove_job(tmp_path, worktree)
    job.kwargs["expected_detached"] = False
    job.kwargs["expected_branch"] = "7-auto"
    job.kwargs["local_branch_cleanup"] = {
        "branch": "7-auto",
        "base_sha": "a" * 40,
    }
    records = [
        {
            "path": str(worktree),
            "branch": "refs/heads/7-auto",
            "commit": "a" * 40,
        }
    ]

    def run(command: list[str], **kwargs: object) -> SimpleNamespace:
        if command[:3] == ["git", "worktree", "remove"]:
            worktree.rmdir()
        return SimpleNamespace(stdout="")

    with (
        patch.object(WorktreeManager, "list_worktrees", side_effect=[records, []]),
        patch("hephaestus.automation.pipeline.git_cleanup.run", side_effect=run) as git_run,
        patch(
            "hephaestus.automation.pipeline.git_cleanup.delete_local_branch_if_unchanged",
            side_effect=[OSError("branch database unavailable"), True],
        ) as delete_branch,
    ):
        with pytest.raises(OSError, match="branch database unavailable"):
            run_cleanup_job(job)
        result = run_cleanup_job(job)

    assert result == JobResult(
        ok=True,
        value={"local_branch_deleted": True},
    )
    assert delete_branch.call_count == 2
    assert (
        sum(call.args[0][:3] == ["git", "worktree", "remove"] for call in git_run.call_args_list)
        == 1
    )
