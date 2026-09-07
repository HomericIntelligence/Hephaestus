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
    assert "NPM_CONFIG_PACKAGE_LOCK" not in environment
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


def test_codex_implementation_environment_uses_only_receipt_git_values(
    monkeypatch: pytest.MonkeyPatch,
    platform_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """The implementation child gets the exact receipt-owned Git environment."""
    monkeypatch.setenv("GIT_DIR", "/poison/git")
    worktree = tmp_path / "worktree"
    fixed = {
        "GIT_DIR": str(tmp_path / "git-dir"),
        "GIT_WORK_TREE": str(worktree),
        "GIT_INDEX_FILE": str(tmp_path / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }

    environment = child_environments.build_codex_implementation_child_env(
        codex_home=tmp_path / "private-codex",
        fixed_git_environment=fixed,
    )

    assert environment["CODEX_HOME"] == str(tmp_path / "private-codex")
    assert {name: environment[name] for name in fixed} == fixed


def test_codex_implementation_environment_replaces_ambient_private_paths(
    monkeypatch: pytest.MonkeyPatch,
    platform_env: dict[str, str],
    tmp_path: Path,
) -> None:
    """The implementation child uses only profile-owned private directories."""
    private = tmp_path / "private-codex"
    poisoned = str(tmp_path / "operator-state")
    for name in (
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    ):
        monkeypatch.setenv(name, poisoned)
    fixed = {
        "GIT_DIR": str(tmp_path / "git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "worktree"),
        "GIT_INDEX_FILE": str(tmp_path / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }

    environment = child_environments.build_codex_implementation_child_env(
        codex_home=private,
        fixed_git_environment=fixed,
    )

    assert environment["HOME"] == str(private / "home")
    assert {environment[name] for name in ("TMPDIR", "TMP", "TEMP")} == {str(private / "tmp")}
    assert environment["USERPROFILE"] == str(private / "home")
    assert environment["APPDATA"] == str(private / "appdata")
    assert environment["LOCALAPPDATA"] == str(private / "localappdata")
    assert environment["XDG_CONFIG_HOME"] == str(private / "xdg" / "config")
    assert environment["XDG_CACHE_HOME"] == str(private / "xdg" / "cache")
    assert environment["XDG_DATA_HOME"] == str(private / "xdg" / "data")
    assert poisoned not in environment.values()


def test_codex_implementation_environment_rejects_incomplete_git_receipt(
    platform_env: dict[str, str], tmp_path: Path
) -> None:
    """The child environment rejects a partial Git authority set."""
    with pytest.raises(ValueError, match="fixed Git environment"):
        child_environments.build_codex_implementation_child_env(
            codex_home=tmp_path / "private-codex",
            fixed_git_environment={"GIT_DIR": str(tmp_path / "git-dir")},
        )


def test_codex_implementation_environment_rejects_enabled_replace_objects(
    platform_env: dict[str, str], tmp_path: Path
) -> None:
    """The child environment rejects enabled Git replacement objects."""
    fixed = {
        "GIT_DIR": str(tmp_path / "git-dir"),
        "GIT_WORK_TREE": str(tmp_path / "worktree"),
        "GIT_INDEX_FILE": str(tmp_path / "index"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "0",
    }

    with pytest.raises(ValueError, match="invalid value"):
        child_environments.build_codex_implementation_child_env(
            codex_home=tmp_path / "private-codex",
            fixed_git_environment=fixed,
        )


@pytest.fixture
def nested_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    """Install the finite host verification environment."""
    scratch = tmp_path / "scratch"
    values = child_environments.build_host_verification_env(
        home=scratch / "home",
        temporary=scratch / "tmp",
        cache=scratch / "cache",
        runtime_environment=tmp_path / "runtime",
        executable=tmp_path / "bin" / "uv",
    )
    monkeypatch.setattr(os, "environ", values)
    return values


def test_nested_host_verification_preserves_registered_values(
    nested_environment: dict[str, str],
    tmp_path: Path,
) -> None:
    """Only the named host values and source path reach the child."""
    expected = {
        name: value for name, value in nested_environment.items() if name != "PYTEST_CURRENT_TEST"
    }
    nested_environment["GH_TOKEN"] = "private-test-value"  # noqa: S105 - test sentinel
    nested_environment["UNRELATED"] = "unrelated"
    source = tmp_path / "source"
    environment = child_environments.build_nested_host_verification_env(source)
    assert environment == {**expected, "PYTHONPATH": str(source.resolve())}
    assert all(
        validate_environment_value(APPROVED_ENV_BY_NAME[n], v) for n, v in environment.items()
    )


@pytest.mark.parametrize(
    "name",
    [
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_CACHE_HOME",
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_OFFLINE",
        "UV_NO_SYNC",
        "RUFF_CACHE_DIR",
        "COVERAGE_FILE",
        "PYTHONPYCACHEPREFIX",
        "PYTHONDONTWRITEBYTECODE",
        "PYTEST_ADDOPTS",
        "PATH",
    ],
)
def test_nested_host_verification_missing_required_value(
    nested_environment: dict[str, str],
    tmp_path: Path,
    name: str,
) -> None:
    """An absent required value stops environment transfer."""
    del nested_environment[name]
    with pytest.raises(ValueError, match=r"^Invalid host verification environment[.]$"):
        child_environments.build_nested_host_verification_env(tmp_path / "source")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COVERAGE_FILE", "relative"),
        ("COVERAGE_FILE", "/outside/.coverage"),
        ("TMP", "/outside/tmp"),
        ("UV_OFFLINE", "0"),
        ("UV_NO_SYNC", "true"),
        ("PYTHONDONTWRITEBYTECODE", "0"),
        ("PYTEST_ADDOPTS", "--override-ini=addopts="),
        ("LANG", "private\0value"),
        ("PATH", "private\0value"),
        ("RUFF_CACHE_DIR", "/outside/ruff"),
        ("UV_CACHE_DIR", "/outside/uv"),
        ("PYTHONPYCACHEPREFIX", "/outside/pycache"),
    ],
)
def test_nested_host_verification_invalid_value(
    nested_environment: dict[str, str],
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    """Invalid values and paths cannot pass the environment boundary."""
    nested_environment[name] = value
    with pytest.raises(ValueError, match=r"^Invalid host verification environment[.]$"):
        child_environments.build_nested_host_verification_env(tmp_path / "source")


@pytest.mark.parametrize(
    "relation",
    ["source_contains_scratch", "scratch_contains_source", "runtime_in_source", "symlink"],
)
def test_nested_host_verification_rejects_source_and_containment(
    nested_environment: dict[str, str],
    tmp_path: Path,
    relation: str,
) -> None:
    """Source paths cannot become writable output paths."""
    source = tmp_path / "source"
    if relation == "source_contains_scratch":
        source = tmp_path
    elif relation == "scratch_contains_source":
        source = tmp_path / "scratch" / "source"
    elif relation == "runtime_in_source":
        nested_environment["UV_PROJECT_ENVIRONMENT"] = str(source / ".venv")
    else:
        cache = tmp_path / "scratch" / "cache"
        cache.mkdir(parents=True)
        source.mkdir()
        (cache / ".coverage").symlink_to(source / ".coverage")
    with pytest.raises(ValueError, match=r"^Invalid host verification environment[.]$"):
        child_environments.build_nested_host_verification_env(source)
