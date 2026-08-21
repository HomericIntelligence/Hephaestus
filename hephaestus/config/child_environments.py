"""Finite, named environment builders for subprocess boundaries."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from hephaestus.config.environment_registry import (
    APPROVED_ENV_BY_NAME,
    validate_environment_value,
)


def _absolute_path(path: Path) -> str:
    """Return an explicit path in the canonical child-boundary form."""
    return str(path.expanduser().absolute())


def read_approved_parent_env() -> dict[str, str]:
    """Read the exact non-secret host substrate admitted by policy."""
    values = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "USER": os.environ.get("USER", ""),
        "LOGNAME": os.environ.get("LOGNAME", ""),
        "SHELL": os.environ.get("SHELL", ""),
        "LANG": os.environ.get("LANG", ""),
        "LC_ALL": os.environ.get("LC_ALL", ""),
        "LC_CTYPE": os.environ.get("LC_CTYPE", ""),
        "TZ": os.environ.get("TZ", ""),
        "TMPDIR": os.environ.get("TMPDIR", ""),
        "TMP": os.environ.get("TMP", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "XDG_CONFIG_HOME": os.environ.get("XDG_CONFIG_HOME", ""),
        "XDG_CACHE_HOME": os.environ.get("XDG_CACHE_HOME", ""),
        "XDG_DATA_HOME": os.environ.get("XDG_DATA_HOME", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),  # noqa: SIM112 - Windows alias
        "WINDIR": os.environ.get("WINDIR", ""),
        "ComSpec": os.environ.get("ComSpec", ""),  # noqa: SIM112 - Windows alias
        "COMSPEC": os.environ.get("COMSPEC", ""),
        "PATHEXT": os.environ.get("PATHEXT", ""),
    }
    env = {
        name: value
        for name, value in values.items()
        if value and validate_environment_value(APPROVED_ENV_BY_NAME[name], value)
    }
    env.setdefault("PATH", os.defpath)
    return env


def _platform_env() -> dict[str, str]:
    return read_approved_parent_env()


def build_claude_child_env() -> dict[str, str]:
    """Build the Claude CLI environment without ambient provider configuration."""
    env = _platform_env()
    env["CLAUDECODE"] = ""
    return env


def build_codex_child_env(*, codex_home: Path | None = None) -> dict[str, str]:
    """Build the Codex CLI environment from an explicit or derived home path."""
    env = _platform_env()
    if codex_home is None:
        home = env.get("HOME")
        codex_home = Path(home) / ".codex" if home else Path.cwd() / ".codex"
    env["CODEX_HOME"] = _absolute_path(codex_home)
    return env


def build_pi_child_env(
    *, temp_dir: Path | None = None, pi_dir: Path | None = None
) -> dict[str, str]:
    """Build the Pi environment from explicit configuration only."""
    env = _platform_env()
    for name in ("TMPDIR", "TMP", "TEMP"):
        env.pop(name, None)
    if temp_dir is not None:
        for name in ("TMPDIR", "TMP", "TEMP"):
            env[name] = _absolute_path(temp_dir)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "PI_TELEMETRY": "0",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    if pi_dir is not None:
        env["PI_CODING_AGENT_DIR"] = _absolute_path(pi_dir)
    return env


def build_gh_child_env() -> dict[str, str]:
    """Build the GitHub CLI environment with only its two auth bridges."""
    env = _platform_env()
    gh_token = os.environ.get("GH_TOKEN", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if validate_environment_value(APPROVED_ENV_BY_NAME["GH_TOKEN"], gh_token):
        env["GH_TOKEN"] = gh_token
    if validate_environment_value(APPROVED_ENV_BY_NAME["GITHUB_TOKEN"], github_token):
        env["GITHUB_TOKEN"] = github_token
    return env


def build_git_child_env(*, global_config: Path | None = None) -> dict[str, str]:
    """Build a non-interactive Git environment with configuration injection disabled."""
    env = _platform_env()
    env.update(
        {
            "GIT_CONFIG_GLOBAL": _absolute_path(global_config) if global_config else os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def build_git_signing_env(*, global_config: Path | None = None) -> dict[str, str]:
    """Build a Git environment that admits only explicit host signing bridges."""
    env = build_git_child_env(global_config=global_config)
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK", "")
    gpg_tty = os.environ.get("GPG_TTY", "")
    if validate_environment_value(APPROVED_ENV_BY_NAME["SSH_AUTH_SOCK"], ssh_auth_sock):
        env["SSH_AUTH_SOCK"] = ssh_auth_sock
    if validate_environment_value(APPROVED_ENV_BY_NAME["GPG_TTY"], gpg_tty):
        env["GPG_TTY"] = gpg_tty
    return env


def build_host_verification_env(
    *,
    home: Path,
    temporary: Path,
    cache: Path,
    runtime_environment: Path,
    executable: Path,
    git_executable: Path | None = None,
) -> dict[str, str]:
    """Build the offline, isolated environment for host verification."""
    path_parts = [str(executable.expanduser().absolute().parent)]
    if git_executable is not None:
        path_parts.append(str(git_executable.expanduser().absolute().parent))
    path_parts.append(os.defpath)
    locale = read_approved_parent_env()
    return {
        **{name: locale[name] for name in ("LANG", "LC_ALL") if name in locale},
        "HOME": _absolute_path(home),
        "TMPDIR": _absolute_path(temporary),
        "TMP": _absolute_path(temporary),
        "TEMP": _absolute_path(temporary),
        "XDG_CACHE_HOME": _absolute_path(cache),
        "UV_CACHE_DIR": _absolute_path(cache / "uv"),
        "UV_PROJECT_ENVIRONMENT": _absolute_path(runtime_environment),
        "UV_OFFLINE": "1",
        "UV_NO_SYNC": "1",
        "RUFF_CACHE_DIR": _absolute_path(cache / "ruff"),
        "COVERAGE_FILE": _absolute_path(cache / ".coverage"),
        "PYTHONPYCACHEPREFIX": _absolute_path(cache / "pycache"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
        "PATH": os.pathsep.join(path_parts),
    }


def build_sbatch_submission_env() -> dict[str, str]:
    """Build the minimal host environment needed to locate and submit sbatch."""
    return _platform_env()


def build_python_phase_env(repo_root: Path) -> dict[str, str]:
    """Build a Python phase environment scoped to one source checkout."""
    env = _platform_env()
    env["PYTHONPATH"] = str(repo_root.resolve())
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def with_correlation_id(environment: Mapping[str, str], trace_id: str | None) -> dict[str, str]:
    """Return a copied environment with a validated optional GitHub trace ID."""
    env = dict(environment)
    if trace_id:
        if "\0" in trace_id or not trace_id.strip():
            raise ValueError("trace_id must be a non-empty token without NUL")
        env["GH_TRACE_ID"] = trace_id
    return env


__all__ = [
    "build_claude_child_env",
    "build_codex_child_env",
    "build_gh_child_env",
    "build_git_child_env",
    "build_git_signing_env",
    "build_host_verification_env",
    "build_pi_child_env",
    "build_python_phase_env",
    "build_sbatch_submission_env",
    "read_approved_parent_env",
    "with_correlation_id",
]
