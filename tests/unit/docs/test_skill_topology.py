"""Regression tests for externalized skill/plugin topology (#2134, #2504)."""

import json
import re
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_GUIDES = (REPO_ROOT / "AGENTS.md",)
TOPOLOGY_FILES = (
    *SKILL_GUIDES,
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / ".markdownlint.yaml",
)

LOCAL_SKILLS_PATH = re.compile(r"(?<![\w.-])(?:\./|Hephaestus/)?skills(?=[/\\`)])")
LEGACY_PLUGIN_PATHS = (
    REPO_ROOT / "skills",
    REPO_ROOT / "plugins" / "hephaestus",
    REPO_ROOT / ".codex-plugin",
)
REQUIRED_ROOT_METADATA = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "SECURITY.md",
    REPO_ROOT / "LICENSE",
    REPO_ROOT / ".codexignore",
)
ATHENA_REPOSITORY = "https://github.com/HomericIntelligence/Athena.git"


def _present_path_names(paths: tuple[Path, ...], root: Path) -> list[str]:
    """Return repository-relative names for entries that exist or are symlinks."""
    return [
        path.relative_to(root).as_posix() for path in paths if path.exists() or path.is_symlink()
    ]


def _enabled_plugins_from_marketplace(enabled: Mapping[str, object], marketplace: str) -> list[str]:
    """Return enabled plugins for a marketplace without case-sensitive bypasses."""
    return sorted(
        name
        for name, value in enabled.items()
        if value is True and name.rpartition("@")[2].casefold() == marketplace.casefold()
    )


def test_repository_has_no_local_plugin_distribution() -> None:
    """Retired Hephaestus plugin roots must not be recreated."""
    present = _present_path_names(LEGACY_PLUGIN_PATHS, REPO_ROOT)

    assert present == [], "repository-local plugin content must not reappear: " + ", ".join(present)


def test_dangling_legacy_plugin_symlink_remains_present(tmp_path: Path) -> None:
    """A broken symlink is still a recreated legacy plugin-root entry."""
    legacy_root = tmp_path / "skills"
    legacy_root.symlink_to(tmp_path / "retired-target")

    assert _present_path_names((legacy_root,), tmp_path) == ["skills"]


def test_repository_metadata_remains_at_root() -> None:
    """Repository policy files replace the retired nested plugin copies."""
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in REQUIRED_ROOT_METADATA
        if not path.is_file()
    ]

    assert missing == [], "missing root metadata: " + ", ".join(missing)


def test_settings_route_skill_plugins_to_athena() -> None:
    """Hephaestus must consume skills from its configured marketplace."""
    settings = json.loads((REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    enabled = settings["enabledPlugins"]
    marketplace = settings["extraKnownMarketplaces"]["Athena"]["source"]

    assert marketplace == {
        "source": "git",
        "url": ATHENA_REPOSITORY,
    }
    assert any(
        name.rpartition("@")[2].casefold() == "athena" and value is True
        for name, value in enabled.items()
    )
    assert _enabled_plugins_from_marketplace(enabled, "hephaestus") == []


def test_legacy_marketplace_suffix_is_case_insensitive() -> None:
    """Case variations must not restore the retired Hephaestus marketplace."""
    enabled = {
        "skill@Athena": True,
        "safety-net@cc-marketplace": True,
        "legacy@Hephaestus": True,
    }

    assert _enabled_plugins_from_marketplace(enabled, "hephaestus") == ["legacy@Hephaestus"]


def test_repository_has_no_local_skills_directory() -> None:
    """The documented plugin topology must not depend on a local skills path."""
    local_skills = REPO_ROOT / "skills"

    assert not local_skills.exists(), (
        "repository-local skills content must not reappear without updating "
        "the documented plugin topology"
    )


def test_topology_files_have_no_local_skill_path_references() -> None:
    """Reject local paths including skills/, ./skills/, links, and code spans."""
    for document in TOPOLOGY_FILES:
        text = document.read_text(encoding="utf-8")
        assert LOCAL_SKILLS_PATH.search(text) is None, (
            f"{document.relative_to(REPO_ROOT)} references a repository-local skills path"
        )


def test_skill_guides_identify_the_plugin_source_of_truth() -> None:
    """Agent guides must direct readers to the enabled Athena plugins."""
    for document in SKILL_GUIDES:
        text = document.read_text(encoding="utf-8")
        assert ".claude/settings.json" in text
        assert "Athena" in text
        assert "plugin" in text.lower()


def test_markdownlint_prohibits_inline_html_without_an_allow_list() -> None:
    """The topology no longer needs skill-specific inline-HTML exemptions."""
    config = (REPO_ROOT / ".markdownlint.yaml").read_text(encoding="utf-8")

    assert "MD033: true" in config
    assert "allowed_elements" not in config
