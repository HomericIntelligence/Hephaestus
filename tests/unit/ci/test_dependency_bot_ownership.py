"""Guard dependency-bot ownership boundaries for Renovate and Dependabot."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEPENDABOT_OWNED_RENOVATE_MANAGERS = {"pep621", "pip_requirements", "pip-requirements"}


def _renovate_config() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((REPO_ROOT / "renovate.json").read_text()))


def _dependabot_config() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text()),
    )


def test_renovate_is_limited_to_pixi_manager() -> None:
    """Renovate must not manage pip/PEP 621 deps owned by Dependabot."""
    config = _renovate_config()

    assert config["enabledManagers"] == ["pixi"]
    assert not (set(config["enabledManagers"]) & DEPENDABOT_OWNED_RENOVATE_MANAGERS)


def test_renovate_package_rules_only_target_enabled_managers() -> None:
    """Package rules must not reintroduce disabled managers."""
    config = _renovate_config()
    enabled_managers = set(config["enabledManagers"])

    for rule in config.get("packageRules", []):
        managers = set(rule.get("matchManagers", []))
        assert managers <= enabled_managers, (
            f"packageRule {rule.get('description', rule)!r} targets disabled managers: "
            f"{sorted(managers - enabled_managers)}"
        )


def test_dependabot_owns_root_pip_ecosystem() -> None:
    """The pip ecosystem remains assigned to Dependabot at the repository root."""
    updates = _dependabot_config()["updates"]

    assert any(
        update.get("package-ecosystem") == "pip" and update.get("directory") == "/"
        for update in updates
    )
