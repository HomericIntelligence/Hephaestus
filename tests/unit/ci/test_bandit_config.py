"""Tests for bandit SAST configuration."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]


class TestBanditConfig:
    """Tests for bandit SAST configuration across pixi, pyproject, workflows, and pre-commit."""

    @staticmethod
    def _get_repo_root() -> Path:
        """Get the repository root directory."""
        test_dir = Path(__file__).parent
        return test_dir.parent.parent.parent

    def test_bandit_in_lint_pypi_dependencies(self) -> None:
        """Bandit must be declared in [feature.lint.pypi-dependencies]."""
        repo_root = self._get_repo_root()
        pixi_toml = repo_root / "pixi.toml"
        content = pixi_toml.read_text(encoding="utf-8")
        assert "[feature.lint.pypi-dependencies]" in content
        assert "bandit" in content
        # Verify constraint is set (not just "bandit" without version)
        assert "bandit =" in content or "bandit >" in content or "bandit <" in content

    def test_sast_task_exists_in_pixi(self) -> None:
        """Pixi must define a sast task in [feature.lint.tasks]."""
        repo_root = self._get_repo_root()
        pixi_toml = repo_root / "pixi.toml"
        content = pixi_toml.read_text(encoding="utf-8")
        assert "sast =" in content
        assert "bandit" in content

    def test_sast_task_scopes_correctly(self) -> None:
        """The sast task must scan both hephaestus/ and scripts/."""
        repo_root = self._get_repo_root()
        pixi_toml = repo_root / "pixi.toml"
        content = pixi_toml.read_text(encoding="utf-8")
        # Extract sast task line
        lines = content.split("\n")
        sast_line = None
        for line in lines:
            if "sast =" in line:
                sast_line = line
                break
        assert sast_line is not None
        assert "hephaestus" in sast_line
        assert "scripts" in sast_line
        assert "bandit" in sast_line

    def test_tool_bandit_section_exists(self) -> None:
        """pyproject.toml must have a [tool.bandit] section."""
        repo_root = self._get_repo_root()
        pyproject = repo_root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)
        assert "tool" in config
        assert "bandit" in config["tool"]

    def test_bandit_config_excludes_tests_and_build(self) -> None:
        """The [tool.bandit] section must exclude tests, build, and .pixi."""
        repo_root = self._get_repo_root()
        pyproject = repo_root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)
        bandit_cfg = config["tool"]["bandit"]
        assert "exclude_dirs" in bandit_cfg
        exclude = bandit_cfg["exclude_dirs"]
        assert "tests" in exclude
        assert "build" in exclude
        assert ".pixi" in exclude

    def test_no_b6xx_skips_in_config(self) -> None:
        """The [tool.bandit] must not globally skip B602/B603/B604/B607 injection checks."""
        repo_root = self._get_repo_root()
        pyproject = repo_root / "pyproject.toml"
        with open(pyproject, "rb") as f:
            config = tomllib.load(f)
        bandit_cfg = config["tool"]["bandit"]
        # Should not have a skips entry, or if it does, it should not contain B6xx injection checks
        if "skips" in bandit_cfg:
            skips = bandit_cfg["skips"]
            for check in ["B602", "B603", "B604", "B607"]:
                assert check not in skips, (
                    f"{check} must not be globally skipped (injection checks are the point of SAST)"
                )

    def test_security_sast_scan_job_in_required_workflow(self) -> None:
        """The _required.yml workflow must have a security-sast-scan job."""
        repo_root = self._get_repo_root()
        required_yml = repo_root / ".github" / "workflows" / "_required.yml"
        with open(required_yml, encoding="utf-8") as f:
            workflow = yaml.safe_load(f)
        assert "jobs" in workflow
        assert "security-sast-scan" in workflow["jobs"]
        job = workflow["jobs"]["security-sast-scan"]
        assert job["name"] == "security/sast-scan"

    def test_bandit_hook_in_precommit_config(self) -> None:
        """The .pre-commit-config.yaml must have a bandit hook."""
        repo_root = self._get_repo_root()
        precommit_cfg = repo_root / ".pre-commit-config.yaml"
        with open(precommit_cfg, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        # Find the bandit hook
        bandit_hook = None
        for repo in config["repos"]:
            if repo.get("repo") == "local":
                for hook in repo.get("hooks", []):
                    if hook.get("id") == "bandit":
                        bandit_hook = hook
                        break
        assert bandit_hook is not None, "bandit hook must exist in .pre-commit-config.yaml"

    def test_bandit_hook_runs_automatically(self) -> None:
        """The bandit hook must NOT have stages: [manual] (runs automatically)."""
        repo_root = self._get_repo_root()
        precommit_cfg = repo_root / ".pre-commit-config.yaml"
        with open(precommit_cfg, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        # Find the bandit hook
        bandit_hook = None
        for repo in config["repos"]:
            if repo.get("repo") == "local":
                for hook in repo.get("hooks", []):
                    if hook.get("id") == "bandit":
                        bandit_hook = hook
                        break
        assert bandit_hook is not None
        # Must NOT have stages: [manual] or any stages restriction
        stages = bandit_hook.get("stages")
        assert stages is None or "manual" not in stages, (
            "bandit hook must run automatically, not in manual stages"
        )

    def test_no_check_shell_injection_hook(self) -> None:
        """The check-shell-injection pygrep hook must be removed (replaced by bandit)."""
        repo_root = self._get_repo_root()
        precommit_cfg = repo_root / ".pre-commit-config.yaml"
        with open(precommit_cfg, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        # Search for check-shell-injection hook — should not exist
        for repo in config["repos"]:
            if repo.get("repo") == "local":
                for hook in repo.get("hooks", []):
                    assert hook.get("id") != "check-shell-injection", (
                        "check-shell-injection hook must be removed (bandit supersedes it)"
                    )

    def test_bandit_low_baseline_file_exists_and_parses(self) -> None:
        """The LOW-severity baseline JSON must exist and parse as {test_id: int}."""
        repo_root = self._get_repo_root()
        baseline = repo_root / "hephaestus" / "ci" / "bandit_low_baseline.json"
        assert baseline.exists()
        data = json.loads(baseline.read_text(encoding="utf-8"))
        assert "counts" in data
        assert all(isinstance(v, int) for v in data["counts"].values())

    def test_security_workflow_runs_low_severity_baseline_check(self) -> None:
        """The weekly security.yml sast job must run the baseline-drift check."""
        repo_root = self._get_repo_root()
        security_yml = repo_root / ".github" / "workflows" / "security.yml"
        content = security_yml.read_text(encoding="utf-8")
        assert "sast-audit-trail" in content

    def test_pixi_sast_audit_trail_task_wired_to_baseline_check(self) -> None:
        """The sast-audit-trail task must invoke bandit_baseline_check.py at severity-level low."""
        repo_root = self._get_repo_root()
        pixi_toml = repo_root / "pixi.toml"
        content = pixi_toml.read_text(encoding="utf-8")
        assert "sast-audit-trail" in content
        assert "bandit_baseline_check.py" in content
        assert "--severity-level low" in content
