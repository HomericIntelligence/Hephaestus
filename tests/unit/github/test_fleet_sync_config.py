"""Unit tests for fleet_sync config resolution (issue #716)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from hephaestus.github.fleet_sync import resolve_fleet_config


class TestResolveFleetConfig:
    """Tests for resolve_fleet_config() layered resolution chain."""

    def test_cli_args_override_everything(self, monkeypatch, tmp_path) -> None:
        """CLI args have highest priority."""
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: FromFile\nrepos: [a, b]\n")
        monkeypatch.setenv("FLEET_ORG", "FromEnv")
        monkeypatch.setenv("FLEET_REPOS", "x,y")
        org, repos = resolve_fleet_config(
            cli_org="FromCli", cli_repos=["p", "q"], config_path=str(cfg)
        )
        assert org == "FromCli"
        assert repos == ["p", "q"]

    def test_env_overrides_file(self, monkeypatch, tmp_path) -> None:
        """Environment variables override config file."""
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: FromFile\nrepos: [a, b]\n")
        monkeypatch.setenv("FLEET_ORG", "FromEnv")
        monkeypatch.setenv("FLEET_REPOS", "x,y")
        org, repos = resolve_fleet_config(cli_org=None, cli_repos=None, config_path=str(cfg))
        assert org == "FromEnv"
        assert repos == ["x", "y"]

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

    def test_env_repos_comma_split(self, monkeypatch, tmp_path) -> None:
        """FLEET_REPOS is comma-separated with whitespace trimmed."""
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", "r1, r2 ,r3")
        _, repos = resolve_fleet_config(
            cli_org=None,
            cli_repos=None,
            config_path=str(tmp_path / "nonexistent.yml"),
        )
        assert repos == ["r1", "r2", "r3"]

    def test_env_repos_numeric_not_coerced(self, monkeypatch, tmp_path) -> None:
        """FLEET_REPOS=123,456 stays as strings, not converted to int (R0-Major regression)."""
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", "123,456")
        _, repos = resolve_fleet_config(
            cli_org=None,
            cli_repos=None,
            config_path=str(tmp_path / "nonexistent.yml"),
        )
        assert repos == ["123", "456"]
        assert all(isinstance(r, str) for r in repos)

    def test_env_repos_empty_after_split_raises(self, monkeypatch, tmp_path) -> None:
        """FLEET_REPOS set but with no valid entries fails with the differentiated error.

        The matcher pins the FLEET_REPOS-specific diagnostic (``FLEET_REPOS is set``)
        rather than the substring ``FLEET_REPOS``, which the generic fallback message
        ("no fleet repos configured. Set --repos, FLEET_REPOS, ...") would also satisfy.
        """
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", " , , ")
        with pytest.raises(RuntimeError, match=r"FLEET_REPOS is set but contains no valid entries"):
            resolve_fleet_config(
                cli_org=None,
                cli_repos=None,
                config_path=str(tmp_path / "nope.yml"),
            )

    def test_env_repos_empty_string_raises_differentiated_error(
        self, monkeypatch, tmp_path
    ) -> None:
        """FLEET_REPOS='' (set but empty) hits the differentiated error, not the generic one.

        ``FLEET_REPOS=''`` follows the set-but-empty path (raw is ``''`` not ``None``),
        so operators must see the FLEET_REPOS-specific diagnostic to tell it apart from
        leaving the variable unset.
        """
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", "")
        with pytest.raises(RuntimeError, match=r"FLEET_REPOS is set but contains no valid entries"):
            resolve_fleet_config(
                cli_org=None,
                cli_repos=None,
                config_path=str(tmp_path / "nope.yml"),
            )

    def test_cli_repos_override_bad_env_repos(self, monkeypatch, tmp_path) -> None:
        """An explicit --repos overrides a malformed FLEET_REPOS without erroring.

        The differentiated FLEET_REPOS error must NOT fire when a higher-priority CLI
        value is present, since the bad env var is irrelevant in that case.
        """
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", " , , ")
        org, repos = resolve_fleet_config(
            cli_org=None,
            cli_repos=["repoX"],
            config_path=str(tmp_path / "nope.yml"),
        )
        assert org == "Org"
        assert repos == ["repoX"]

    def test_missing_config_file_falls_through_to_env(self, monkeypatch, tmp_path) -> None:
        """Missing config file does not raise — falls through to env vars."""
        monkeypatch.setenv("FLEET_ORG", "Org")
        monkeypatch.setenv("FLEET_REPOS", "r1")
        org, repos = resolve_fleet_config(
            cli_org=None,
            cli_repos=None,
            config_path=str(tmp_path / "nope.yml"),
        )
        assert org == "Org"
        assert repos == ["r1"]

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

        monkeypatch.setattr("hephaestus.config.utils.YAML_AVAILABLE", False)
        cfg = tmp_path / ".fleet.yml"
        cfg.write_text("org: acme\nrepos: [a, b]\n")
        with pytest.raises(RuntimeError, match=r"Failed to load fleet config from .*\.fleet\.yml"):
            fleet_sync._load_fleet_config(str(cfg))
