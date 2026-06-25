"""Regression tests for Codex catalog listing requirements."""

from __future__ import annotations

import json
from pathlib import Path

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
    assert (plugin_root / ".codex-plugin" / "plugin.json").resolve() == (
        REPO_ROOT / ".codex-plugin" / "plugin.json"
    ).resolve()
    assert (plugin_root / "assets").is_symlink()
    assert (plugin_root / "assets" / "icon.svg").resolve() == (
        REPO_ROOT / "assets" / "icon.svg"
    ).resolve()
    assert (plugin_root / ".codexignore").is_symlink()
    assert (plugin_root / ".codexignore").resolve() == (REPO_ROOT / ".codexignore").resolve()


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
