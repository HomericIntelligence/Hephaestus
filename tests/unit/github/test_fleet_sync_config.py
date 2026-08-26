"""Unit tests for fleet_sync config resolution (issue #716)."""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest

from hephaestus.github.fleet_sync import resolve_fleet_config


@pytest.fixture
def no_discovered_config():
    """Disable .fleet.yml auto-discovery so env/CLI layers are tested in isolation."""
    with patch("hephaestus.github.fleet_sync.config._find_default_config", return_value=None):
        yield


class TestResolveFleetConfig:
    """Tests for resolve_fleet_config() layered resolution chain."""

    def test_cli_args_override_everything(self, monkeypatch, tmp_path) -> None:
        """CLI args have highest priority and removed env names are inert."""
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: FromFile\nrepos: [a, b]\n")
        monkeypatch.setenv("FLEET_ORG", "FromEnv")
        monkeypatch.setenv("FLEET_REPOS", "x,y")
        org, repos = resolve_fleet_config(
            cli_org="FromCli", cli_repos=["p", "q"], config_path=str(cfg)
        )
        assert org == "FromCli"
        assert repos == ["p", "q"]

    def test_removed_environment_does_not_override_file(self, monkeypatch, tmp_path) -> None:
        """Removed FLEET_* variables are poison values, never config sources."""
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: FromFile\nrepos: [a, b]\n")
        monkeypatch.setenv("FLEET_ORG", "FromEnv")
        monkeypatch.setenv("FLEET_REPOS", "x,y")
        org, repos = resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(cfg))
        assert org == "FromFile"
        assert repos == ["a", "b"]

    def test_file_used_when_no_env_or_cli(self, monkeypatch, tmp_path) -> None:
        """Config file is used when CLI and env are absent."""
        monkeypatch.delenv("FLEET_ORG", raising=False)
        monkeypatch.delenv("FLEET_REPOS", raising=False)
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: FromFile\nrepos: [a, b]\n")
        org, repos = resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(cfg))
        assert org == "FromFile"
        assert repos == ["a", "b"]

    def test_missing_org_raises(self, monkeypatch, tmp_path) -> None:
        """Missing org raises RuntimeError with actionable message."""
        monkeypatch.delenv("FLEET_ORG", raising=False)
        monkeypatch.delenv("FLEET_REPOS", raising=False)
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("repos: [a]\n")
        with pytest.raises(RuntimeError, match="no fleet org configured"):
            resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(cfg))

    def test_missing_repos_raises(self, monkeypatch, tmp_path) -> None:
        """Missing repos raises RuntimeError with actionable message."""
        monkeypatch.delenv("FLEET_ORG", raising=False)
        monkeypatch.delenv("FLEET_REPOS", raising=False)
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: SomeOrg\n")
        with pytest.raises(RuntimeError, match="no fleet repos configured"):
            resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(cfg))

    def test_removed_environment_cannot_supply_missing_config(
        self, monkeypatch, no_discovered_config
    ) -> None:
        """Poison FLEET_* values cannot satisfy required fleet selection."""
        monkeypatch.setenv("FLEET_ORG", "PoisonOrg")
        monkeypatch.setenv("FLEET_REPOS", "poison-repo")
        with pytest.raises(RuntimeError, match="no fleet org configured"):
            resolve_fleet_config(cli_org=None, cli_repos=None, config_path=None)

    def test_explicit_missing_config_file_raises(self, monkeypatch, tmp_path) -> None:
        """An explicit nonexistent --config path fails closed."""
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", "r1")
        missing = tmp_path / "nope.yml"
        with pytest.raises(RuntimeError, match=r"fleet config file not found: .*nope\.yml"):
            resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(missing))

    def test_config_path_none_searches_cwd_then_repo_root(self, monkeypatch, tmp_path) -> None:
        """When config_path is None, searches ./.fleet.yml then repo-root."""
        monkeypatch.delenv("FLEET_ORG", raising=False)
        monkeypatch.delenv("FLEET_REPOS", raising=False)
        # Create a temporary .fleet.yml in tmp_path
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: DiscoveredOrg\nrepos: [repo1, repo2]\n")
        monkeypatch.chdir(tmp_path)

        # Mock _find_default_config to return the tmp_path config file
        # This isolates the test from the development environment and makes it portable
        # to installed packages where the bundled .fleet.yml won't exist
        with patch("hephaestus.github.fleet_sync.config._find_default_config") as mock_find:
            mock_find.return_value = cfg
            org, repos = resolve_fleet_config(cli_org=None, cli_repos=None, config_path=None)

        # Verify the mocked config was loaded
        assert org == "DiscoveredOrg"
        assert repos == ["repo1", "repo2"]

    def test_config_path_none_finds_cwd_file(self, monkeypatch, tmp_path) -> None:
        """config_path=None finds .fleet.yml in CWD if it exists."""
        monkeypatch.delenv("FLEET_ORG", raising=False)
        monkeypatch.delenv("FLEET_REPOS", raising=False)
        (tmp_path / ".fleet.yml").write_text("org: CwdOrg\nrepos: [cwdrepo]\n")
        monkeypatch.chdir(tmp_path)
        org, repos = resolve_fleet_config(cli_org=None, cli_repos=None, config_path=None)
        assert org == "CwdOrg"
        assert repos == ["cwdrepo"]

    def test_fleet_config_missing_pyyaml_wraps_with_context(self, tmp_path, monkeypatch) -> None:
        """A .yaml fleet config with PyYAML absent preserves the path context wrapper.

        Regression for issue #1510: the ValueError→RuntimeError type flip in load_config
        must not cause the 'Failed to load fleet config from {path}' wrapper to be lost.
        """
        from hephaestus.github import fleet_sync

        monkeypatch.setitem(sys.modules, "yaml", None)
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: acme\nrepos: [a, b]\n")
        with pytest.raises(RuntimeError, match=r"Failed to load fleet config from .*\.fleet\.yml"):
            fleet_sync._load_fleet_config(str(cfg))
