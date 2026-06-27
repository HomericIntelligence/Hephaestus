"""Regression tests for Codex catalog listing requirements."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_codex_manifest_has_catalog_icon() -> None:
    """The Codex manifest references a small SVG icon for catalog listing."""
    manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))

    icon_rel = manifest.get("interface", {}).get("composerIcon")

    assert icon_rel == "./assets/icon.svg"
    icon_path = REPO_ROOT / icon_rel.removeprefix("./")
    assert icon_path.is_file()
    assert icon_path.suffix == ".svg"
    assert icon_path.stat().st_size <= 50 * 1024

    icon_text = icon_path.read_text(encoding="utf-8")
    assert "base64" not in icon_text
    assert "TODO" not in icon_text
    assert "PLACEHOLDER" not in icon_text


def test_local_codex_marketplace_wrapper_resolves_manifest_and_icon() -> None:
    """The local marketplace wrapper exposes the manifest and catalog icon."""
    marketplace = json.loads(
        (REPO_ROOT / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    plugin = next(plugin for plugin in marketplace["plugins"] if plugin["name"] == "hephaestus")

    plugin_root = REPO_ROOT / plugin["source"]["path"]
    plugin_manifest = plugin_root / ".codex-plugin" / "plugin.json"
    canonical_manifest = REPO_ROOT / ".codex-plugin" / "plugin.json"
    assert plugin_manifest.is_file()
    assert plugin_manifest.read_bytes() == canonical_manifest.read_bytes()

    icon_in_plugin = plugin_root / "assets" / "icon.svg"
    canonical_icon = REPO_ROOT / "assets" / "icon.svg"
    assert icon_in_plugin.is_file()
    assert icon_in_plugin.read_bytes() == canonical_icon.read_bytes()


def test_codexignore_excludes_generated_and_local_state() -> None:
    """Plugin packaging ignores heavyweight build and local cache directories."""
    ignore_path = REPO_ROOT / ".codexignore"

    assert ignore_path.is_file()
    ignored = set(ignore_path.read_text(encoding="utf-8").splitlines())
    assert {
        ".git/",
        ".pixi/",
        "__pycache__/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".mypy_cache/",
        ".coverage",
        "htmlcov/",
        "dist/",
        "build/",
        "*.egg-info/",
    } <= ignored


def test_plugin_scanner_config_ignores_known_non_plugin_secret_fixtures() -> None:
    """HOL scans the repo root, so scoped false-positive ignores stay explicit."""
    config_path = REPO_ROOT / ".plugin-scanner.toml"

    assert config_path.is_file()
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))

    scanner_config = config.get("scanner", {})
    assert scanner_config.get("ignore_paths") == [
        "scripts/shell/setup_api_key.sh",
        "tests/unit/scripts/test_check_private_denylist.py",
    ]


def test_hol_plugin_scanner_workflow_is_pinned_and_uses_config() -> None:
    """The HOL scanner workflow keeps a strict gate without repo-wide false positives."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "hol-plugin-scanner.yml").read_text(
        encoding="utf-8"
    )

    assert re.search(
        r"uses: hashgraph-online/ai-plugin-scanner-action@[0-9a-f]{40}\b",
        workflow,
    )
    assert 'config: ".plugin-scanner.toml"' in workflow
    assert "fail_on_severity: high" in workflow
