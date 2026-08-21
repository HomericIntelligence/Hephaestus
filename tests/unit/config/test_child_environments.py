"""Tests for finite subprocess environment builders."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hephaestus.config import child_environments
from hephaestus.config.environment_registry import (
    APPROVED_ENV_BY_NAME,
    RETIRED_ENV_NAMES,
    validate_environment_value,
)


@pytest.fixture
def platform_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Install a deterministic, finite host substrate for builder tests."""
    values = {
        "PATH": os.defpath,
        "HOME": str(tmp_path / "home"),
        "LANG": "C.UTF-8",
        "TMPDIR": str(tmp_path / "ambient-tmp"),
    }
    monkeypatch.setattr(child_environments, "read_approved_parent_env", lambda: dict(values))
    return values


@pytest.mark.parametrize(
    "secret_name",
    ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "PRIVATE_TOKEN"],
)
def test_provider_builders_do_not_forward_ambient_credentials(
    monkeypatch: pytest.MonkeyPatch,
    platform_env: dict[str, str],
    secret_name: str,
) -> None:
    """Provider children receive no open-ended parent credential namespace."""
    monkeypatch.setenv(secret_name, "poison-secret")

    environments = (
        child_environments.build_claude_child_env(),
        child_environments.build_codex_child_env(),
        child_environments.build_pi_child_env(),
    )

    assert all(secret_name not in environment for environment in environments)
    assert all(RETIRED_ENV_NAMES.isdisjoint(environment) for environment in environments)


def test_pi_builder_uses_only_explicit_directory_and_temporary_paths(
    monkeypatch: pytest.MonkeyPatch,
    platform_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """Poison Pi and temp variables cannot override typed child values."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/poison/pi")
    monkeypatch.setenv("TMPDIR", "/poison/tmp")
    pi_dir = tmp_path / "pi-agent"
    temporary = tmp_path / "private-tmp"

    environment = child_environments.build_pi_child_env(
        pi_dir=pi_dir,
        temp_dir=temporary,
    )

    assert environment["PI_CODING_AGENT_DIR"] == str(pi_dir)
    assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {str(temporary)}
    assert environment["NPM_CONFIG_IGNORE_SCRIPTS"] == "true"
    assert environment["PI_TELEMETRY"] == "0"
    assert environment["PI_SKIP_VERSION_CHECK"] == "1"


def test_auth_and_signing_bridges_are_boundary_specific(
    monkeypatch: pytest.MonkeyPatch,
    platform_env: dict[str, str],
) -> None:
    """GitHub credentials and signing sockets reach only their named boundary."""
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh.sock")
    monkeypatch.setenv("GPG_TTY", "/dev/ttys001")

    github = child_environments.build_gh_child_env()
    git = child_environments.build_git_child_env()
    signing = child_environments.build_git_signing_env()
    claude = child_environments.build_claude_child_env()

    assert github["GH_TOKEN"] == "gh-token"  # noqa: S105 - test sentinel
    assert github["GITHUB_TOKEN"] == "github-token"  # noqa: S105 - test sentinel
    assert "SSH_AUTH_SOCK" not in github
    assert "GH_TOKEN" not in git and "SSH_AUTH_SOCK" not in git
    assert signing["SSH_AUTH_SOCK"] == "/tmp/ssh.sock"
    assert signing["GPG_TTY"] == "/dev/ttys001"
    assert not {"GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "GPG_TTY"} & claude.keys()


def test_child_builders_emit_only_registered_names(
    platform_env: dict[str, str], tmp_path: Path
) -> None:
    """Every environment name crossing a child boundary is centrally registered."""
    environments = (
        child_environments.build_claude_child_env(),
        child_environments.build_codex_child_env(codex_home=tmp_path / "codex"),
        child_environments.build_pi_child_env(pi_dir=tmp_path / "pi"),
        child_environments.build_gh_child_env(),
        child_environments.build_git_child_env(),
        child_environments.build_git_signing_env(),
        child_environments.build_python_phase_env(tmp_path),
        child_environments.build_sbatch_submission_env(),
    )

    for environment in environments:
        assert environment.keys() <= APPROVED_ENV_BY_NAME.keys()
        assert all(
            validate_environment_value(APPROVED_ENV_BY_NAME[name], value)
            for name, value in environment.items()
        )


def test_parent_builder_rejects_values_that_violate_registered_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Declared absolute-path and NUL checks are enforced at the read boundary."""
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("HOME", "relative/home")
    monkeypatch.setenv("TMPDIR", "relative/tmp")

    environment = child_environments.read_approved_parent_env()

    assert environment["PATH"] == os.defpath
    assert "HOME" not in environment
    assert "TMPDIR" not in environment


def test_correlation_id_is_explicit_validated_and_non_mutating() -> None:
    """Correlation injection copies its input and rejects malformed tokens."""
    source = {"PATH": os.defpath}
    result = child_environments.with_correlation_id(source, "trace-123")

    assert result == {"PATH": os.defpath, "GH_TRACE_ID": "trace-123"}
    assert source == {"PATH": os.defpath}
    with pytest.raises(ValueError, match="non-empty token"):
        child_environments.with_correlation_id(source, "bad\0trace")
