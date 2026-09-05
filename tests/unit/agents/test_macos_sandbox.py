"""Tests for the host-enforced macOS process boundary."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.agents import macos_sandbox


def _executable(path: Path) -> Path:
    """Create one inert executable for command-construction tests."""
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_command_fails_closed_outside_macos(tmp_path: Path) -> None:
    """Unsupported hosts cannot start a process with a weaker boundary."""
    executable = _executable(tmp_path / "provider")

    with (
        patch("hephaestus.agents.macos_sandbox.platform.system", return_value="Linux"),
        pytest.raises(macos_sandbox.MacOSSandboxError, match="macOS"),
    ):
        macos_sandbox.isolated_command(
            command=(str(executable),),
            read_roots=(tmp_path,),
            write_roots=(tmp_path / "profile",),
            sandbox_executable=_executable(tmp_path / "sandbox-exec"),
        )


def test_command_has_a_deny_default_profile_and_exact_roots(tmp_path: Path) -> None:
    """The command permits only declared roots and denies network access."""
    executable = _executable(tmp_path / "provider")
    sandbox_executable = _executable(tmp_path / "sandbox-exec")
    worktree = tmp_path / "worktree"
    profile = tmp_path / "profile"
    worktree.mkdir()
    profile.mkdir()

    with patch("hephaestus.agents.macos_sandbox.platform.system", return_value="Darwin"):
        command = macos_sandbox.isolated_command(
            command=(str(executable), "exec"),
            read_roots=(worktree, profile),
            write_roots=(worktree, profile),
            sandbox_executable=sandbox_executable,
        )

    assert command[:2] == (str(sandbox_executable), "-p")
    assert command[-2:] == (str(executable), "exec")
    policy = command[2]
    assert "(deny default)" in policy
    assert "(deny network*)" in policy
    assert f'(subpath "{worktree}")' in policy
    assert f'(subpath "{profile}")' in policy
    assert f'(allow file-write* (subpath "{worktree}"))' in policy
    assert f'(allow file-write* (subpath "{profile}"))' in policy


def test_command_rejects_overlapping_read_roots(tmp_path: Path) -> None:
    """A redundant nested root cannot silently widen the policy."""
    executable = _executable(tmp_path / "provider")
    sandbox_executable = _executable(tmp_path / "sandbox-exec")
    root = tmp_path / "root"
    child = root / "child"
    root.mkdir()
    child.mkdir()

    with (
        patch("hephaestus.agents.macos_sandbox.platform.system", return_value="Darwin"),
        pytest.raises(macos_sandbox.MacOSSandboxError, match="overlap"),
    ):
        macos_sandbox.isolated_command(
            command=(str(executable),),
            read_roots=(root, child),
            write_roots=(),
            sandbox_executable=sandbox_executable,
        )


def test_command_rejects_symbolic_link_roots(tmp_path: Path) -> None:
    """Symbolic links cannot redirect an approved sandbox root after review."""
    if not hasattr(os, "symlink"):
        pytest.skip("symbolic links are unavailable")
    executable = _executable(tmp_path / "provider")
    sandbox_executable = _executable(tmp_path / "sandbox-exec")
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with (
        patch("hephaestus.agents.macos_sandbox.platform.system", return_value="Darwin"),
        pytest.raises(macos_sandbox.MacOSSandboxError, match="symbolic link"),
    ):
        macos_sandbox.isolated_command(
            command=(str(executable),),
            read_roots=(link,),
            write_roots=(),
            sandbox_executable=sandbox_executable,
        )


def test_command_rejects_symbolic_link_provider_executable(tmp_path: Path) -> None:
    """The host must not resolve a provider link before policy validation."""
    if not hasattr(os, "symlink"):
        pytest.skip("symbolic links are unavailable")
    provider = _executable(tmp_path / "provider")
    provider_link = tmp_path / "provider-link"
    provider_link.symlink_to(provider)
    sandbox_executable = _executable(tmp_path / "sandbox-exec")

    with (
        patch("hephaestus.agents.macos_sandbox.platform.system", return_value="Darwin"),
        pytest.raises(macos_sandbox.MacOSSandboxError, match="symbolic link"),
    ):
        macos_sandbox.isolated_command(
            command=(str(provider_link),),
            read_roots=(tmp_path,),
            write_roots=(),
            sandbox_executable=sandbox_executable,
        )
