"""Configuration resolution for fleet sync."""

from __future__ import annotations

import os
from pathlib import Path

from hephaestus.config.utils import load_config

DEFAULT_FLEET_CONFIG_FILENAME = ".fleet.yml"


def _parse_env_repos(env_repos_raw: str | None) -> list[str] | None:
    """Parse comma-separated FLEET_REPOS, returning None if empty after splitting."""
    if env_repos_raw is None:
        return None
    env_repos = [r.strip() for r in env_repos_raw.split(",") if r.strip()]
    return env_repos if env_repos else None


def _find_default_config() -> Path | None:
    """Return the first existing .fleet.yml in CWD or repo-root, else None."""
    cwd_path = Path.cwd() / DEFAULT_FLEET_CONFIG_FILENAME
    if cwd_path.exists():
        return cwd_path

    repo_root_path = Path(__file__).resolve().parents[3] / DEFAULT_FLEET_CONFIG_FILENAME
    if repo_root_path.exists():
        return repo_root_path

    return None


def _load_fleet_config(config_path: str | None) -> tuple[str | None, list[str] | None]:
    """Load org and repos from config file, auto-discovering if needed.

    Raises:
        RuntimeError: If an explicitly provided ``config_path`` does not exist,
            or if the config file exists but fails to load.

    """
    if config_path is not None and not Path(config_path).exists():
        raise RuntimeError(
            f"fleet config file not found: {config_path}. "
            f"Pass an existing file to --config, or omit it to auto-discover "
            f"{DEFAULT_FLEET_CONFIG_FILENAME}"
        )

    if config_path is None:
        found_config = _find_default_config()
        if found_config is not None:
            config_path = str(found_config)

    file_org = None
    file_repos = None

    if config_path is not None:
        config_path_obj = Path(config_path)
        try:
            cfg = load_config(config_path_obj)
            file_org = cfg.get("org")
            file_repos_raw = cfg.get("repos")
            if isinstance(file_repos_raw, list):
                file_repos = file_repos_raw
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            raise RuntimeError(f"Failed to load fleet config from {config_path}: {e}") from e

    return file_org, file_repos


def resolve_fleet_config(
    cli_org: str | None = None,
    cli_repos: list[str] | None = None,
    config_path: str | None = None,
) -> tuple[str, list[str]]:
    """Resolve fleet organization and repo list with layered config sources."""
    env_org = os.environ.get("FLEET_ORG", "").strip()
    env_repos_raw = os.environ.get("FLEET_REPOS")
    env_repos = _parse_env_repos(env_repos_raw) if env_repos_raw is not None else None

    file_org, file_repos = _load_fleet_config(config_path)

    final_org = cli_org or env_org or file_org
    if not final_org:
        raise RuntimeError("no fleet org configured. Set --org, FLEET_ORG, or org: in .fleet.yml")

    if not cli_repos and env_repos_raw is not None and env_repos is None:
        raise RuntimeError(
            f"FLEET_REPOS is set but contains no valid entries after comma-split "
            f"(got {env_repos_raw!r})"
        )

    final_repos = cli_repos or env_repos or file_repos

    if not final_repos:
        raise RuntimeError(
            "no fleet repos configured. Set --repos, FLEET_REPOS, or repos: in .fleet.yml"
        )

    return final_org, final_repos
