"""Behavior tests for the direct-provider host process boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents import process_isolation


def _executable(path: Path) -> Path:
    """Create a harmless regular executable for command construction tests."""
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_macos_isolated_command_writes_a_deny_by_default_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Declared roots are the only worktree and profile filesystem grants."""
    worktree = tmp_path / "worktree"
    profile = tmp_path / "profile"
    worktree.mkdir()
    profile.mkdir()
    executable = _executable(tmp_path / "codex")
    monkeypatch.setattr("hephaestus.agents.process_isolation.sys.platform", "darwin")
    monkeypatch.setattr(process_isolation, "_SYSTEM_READ_ROOTS", ())
    monkeypatch.setattr(process_isolation, "_sandbox_string", lambda path: str(path))
    monkeypatch.setattr("pathlib.Path.is_file", lambda path: True)
    monkeypatch.setattr("hephaestus.agents.process_isolation.os.access", lambda *_args: True)

    command = process_isolation.macos_isolated_command(
        [str(executable), "exec"],
        read_roots=(worktree, profile),
        write_roots=(worktree, profile),
        profile_directory=profile,
        allow_network=True,
    )

    assert command[:3] == ["/usr/bin/sandbox-exec", "-f", str(profile / "codex-automation.sb")]
    content = (profile / "codex-automation.sb").read_text(encoding="utf-8")
    assert "(deny default)" in content
    assert f'(subpath "{worktree}")' in content
    assert f'(subpath "{profile}")' in content
    assert "(allow network*)" in content


def test_macos_isolated_command_fails_closed_without_supported_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unavailable host boundary must prevent provider startup."""
    monkeypatch.setattr("hephaestus.agents.process_isolation.sys.platform", "linux")
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(process_isolation.ProcessIsolationError, match="unavailable"):
        process_isolation.macos_isolated_command(
            ["/usr/bin/true"],
            read_roots=(root,),
            write_roots=(root,),
            profile_directory=root,
            allow_network=False,
        )


def test_macos_isolated_command_rejects_overlapping_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A broad parent root cannot silently include another declared root."""
    root = tmp_path / "root"
    nested = root / "nested"
    profile = tmp_path / "profile"
    root.mkdir()
    nested.mkdir()
    profile.mkdir()
    executable = _executable(tmp_path / "codex")
    monkeypatch.setattr("hephaestus.agents.process_isolation.sys.platform", "darwin")
    monkeypatch.setattr("pathlib.Path.is_file", lambda path: True)
    monkeypatch.setattr("hephaestus.agents.process_isolation.os.access", lambda *_args: True)

    with pytest.raises(process_isolation.ProcessIsolationError, match="overlapping"):
        process_isolation.macos_isolated_command(
            [str(executable)],
            read_roots=(root, nested),
            write_roots=(profile,),
            profile_directory=profile,
            allow_network=False,
        )


def test_sandbox_string_escapes_profile_literals() -> None:
    """Seatbelt literals cannot be broken by a quoted path component."""
    assert process_isolation._sandbox_string(Path('/tmp/a"b\\c')) == '/tmp/a\\"b\\\\c'
