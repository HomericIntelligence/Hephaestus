"""Regression tests for the Codex marketplace plugin payload."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
MARKETPLACE_PATH = REPO_ROOT / ".agents" / "plugins" / "marketplace.json"
CANONICAL_MANIFEST = REPO_ROOT / ".codex-plugin" / "plugin.json"
CANONICAL_SKILLS = REPO_ROOT / "skills"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _regular_files(root: Path) -> list[Path]:
    return sorted(path.relative_to(root) for path in root.rglob("*") if path.is_file())


def _skill_names(skills_dir: Path) -> set[str]:
    return {
        path.parent.name
        for path in skills_dir.glob("*/SKILL.md")
        if path.parent.name != "_repo_analyze_common"
    }


def test_codex_marketplace_payload_is_materialized_and_installable(tmp_path: Path) -> None:
    """The marketplace target must be a self-contained Codex plugin directory."""
    marketplace = _load_json(MARKETPLACE_PATH)
    plugin_entry = next(
        plugin for plugin in marketplace["plugins"] if plugin["name"] == "hephaestus"
    )
    plugin_root = (REPO_ROOT / plugin_entry["source"]["path"]).resolve()
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    plugin_skills = plugin_root / "skills"

    assert plugin_entry["source"]["source"] == "local"
    assert plugin_root == REPO_ROOT / "plugins" / "hephaestus"
    assert plugin_manifest.is_file()
    assert plugin_skills.is_dir()
    assert not [path for path in plugin_root.rglob("*") if path.is_symlink()]

    assert plugin_manifest.read_bytes() == CANONICAL_MANIFEST.read_bytes()
    assert _regular_files(plugin_skills) == _regular_files(CANONICAL_SKILLS)
    for relative_path in _regular_files(CANONICAL_SKILLS):
        assert (plugin_skills / relative_path).read_bytes() == (
            CANONICAL_SKILLS / relative_path
        ).read_bytes()

    shipped_skills = _skill_names(plugin_skills)
    assert shipped_skills == _skill_names(CANONICAL_SKILLS)
    assert {"advise", "learn"} <= shipped_skills

    manifest = _load_json(plugin_manifest)
    cache_dir = tmp_path / "cache" / marketplace["name"] / plugin_entry["name"]
    installed_dir = cache_dir / manifest["version"]
    shutil.copytree(plugin_root, installed_dir)

    assert (installed_dir / ".codex-plugin" / "plugin.json").is_file()
    assert (installed_dir / "skills" / "advise" / "SKILL.md").is_file()
    assert (installed_dir / "skills" / "learn" / "SKILL.md").is_file()
    assert _skill_names(installed_dir / "skills") == shipped_skills
