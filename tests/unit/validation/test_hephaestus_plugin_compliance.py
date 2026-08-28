"""Compliance checks for the nested Hephaestus compatibility distribution."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from hephaestus.agents.frontmatter import extract_frontmatter_parsed, validate_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "hephaestus"
SKILL_ROOT = PLUGIN_ROOT / "skills"

EXPECTED_SKILLS = (
    "advise",
    "brainstorm",
    "code-review",
    "create-reusable-utilities",
    "finish-branch",
    "git-worktrees",
    "github-actions-python-cicd",
    "learn",
    "myrmidon-swarm",
    "python-repo-modernization",
    "repo-analyze",
    "repo-analyze-full",
    "repo-analyze-quick",
    "repo-analyze-quick-full",
    "repo-analyze-strict",
    "repo-analyze-strict-full",
    "review-pr-strict",
    "skill-advisor",
    "systematic-debugging",
    "test-driven-development",
    "tidy",
    "verification",
    "worktree-cleanup",
)

EXPECTED_SKILL_FILES = tuple(SKILL_ROOT / name / "SKILL.md" for name in EXPECTED_SKILLS)
EXPECTED_FRONTMATTER_FIELDS: dict[str, type] = {
    "name": str,
    "description": str,
    "license": str,
}
EXPECTED_OPTIONAL_FIELDS: dict[str, type] = {
    "allowed-tools": list,
    "argument-hint": str,
}
EXPECTED_FRONTMATTER_KEYS = frozenset(EXPECTED_FRONTMATTER_FIELDS) | frozenset(
    EXPECTED_OPTIONAL_FIELDS
)

EXPECTED_PACKAGE_FILES = {
    ".codex-plugin/plugin.json",
    "README.md",
    "LICENSE",
    ".codexignore",
    "assets/icon.svg",
    "skills/THIRD_PARTY_LICENSES.md",
}

EXPECTED_THIRD_PARTY_SKILLS = (
    "test-driven-development",
    "systematic-debugging",
    "verification",
    "git-worktrees",
    "finish-branch",
    "brainstorm",
    "code-review",
    "skill-advisor",
)

PLUGIN_MANIFEST_PATH = PLUGIN_ROOT / ".codex-plugin" / "plugin.json"
PACKAGE_README_PATH = PLUGIN_ROOT / "README.md"
PACKAGE_LICENSE_PATH = PLUGIN_ROOT / "LICENSE"
PACKAGE_IGNORE_PATH = PLUGIN_ROOT / ".codexignore"
THIRD_PARTY_LICENSES_PATH = SKILL_ROOT / "THIRD_PARTY_LICENSES.md"
SETTINGS_PATH = REPO_ROOT / ".claude" / "settings.json"

PLUGIN_MANIFEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "interface",
    ],
    "properties": {
        "name": {"const": "hephaestus"},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "description": {"type": "string", "minLength": 1},
        "author": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "url"],
            "properties": {
                "name": {"const": "HomericIntelligence"},
                "url": {"const": "https://github.com/HomericIntelligence"},
            },
        },
        "homepage": {"const": "https://github.com/HomericIntelligence/ProjectHephaestus"},
        "repository": {"const": "https://github.com/HomericIntelligence/ProjectHephaestus"},
        "license": {"const": "BSD-3-Clause"},
        "keywords": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "skills": {"const": "./skills/"},
        "interface": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "displayName",
                "shortDescription",
                "longDescription",
                "developerName",
                "category",
                "capabilities",
                "websiteURL",
                "defaultPrompt",
                "composerIcon",
            ],
            "properties": {
                "displayName": {"const": "Hephaestus"},
                "shortDescription": {"type": "string", "minLength": 1},
                "longDescription": {"type": "string", "minLength": 1},
                "developerName": {"const": "HomericIntelligence"},
                "category": {"const": "Productivity"},
                "capabilities": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "string", "minLength": 1},
                },
                "websiteURL": {"const": "https://github.com/HomericIntelligence/ProjectHephaestus"},
                "defaultPrompt": {"type": "string", "minLength": 1},
                "composerIcon": {"const": "./assets/icon.svg"},
            },
        },
    },
}


def _load_manifest() -> dict[str, Any]:
    """Return the nested plugin manifest as JSON."""
    return json.loads(PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))


def _read_relative_text(path: Path) -> str:
    """Return path text with the repository UTF-8 encoding contract."""
    return path.read_text(encoding="utf-8")


def test_historical_skill_inventory_is_complete() -> None:
    """The restored package must contain all 23 historical skill manifests."""
    missing = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in EXPECTED_SKILL_FILES
        if not path.is_file()
    ]

    assert missing == [], f"missing skill manifests: {missing}"
    assert "review-pr-strict" in {path.parent.name for path in EXPECTED_SKILL_FILES}


def test_skill_frontmatter_is_safe_and_whitelisted() -> None:
    """Skill manifests must use safe YAML mapping frontmatter with SPDX licenses."""
    for path in EXPECTED_SKILL_FILES:
        parsed = extract_frontmatter_parsed(_read_relative_text(path))
        assert parsed is not None, f"missing frontmatter: {path.relative_to(REPO_ROOT)}"
        _raw, frontmatter = parsed
        assert set(frontmatter).issubset(EXPECTED_FRONTMATTER_KEYS)
        assert frontmatter["license"] == "BSD-3-Clause"
        assert frontmatter["name"] == path.parent.name
        assert (
            validate_frontmatter(
                frontmatter,
                required_fields=EXPECTED_FRONTMATTER_FIELDS,
                optional_fields=EXPECTED_OPTIONAL_FIELDS,
            )
            == []
        )


def test_invalid_frontmatter_is_rejected() -> None:
    """Non-mapping frontmatter must not parse as a skill manifest."""
    assert extract_frontmatter_parsed("---\n- one\n- two\n---\n# body\n") is None


def test_required_package_metadata_files_are_present() -> None:
    """The nested compatibility package must carry its own metadata files."""
    present = {
        path.relative_to(PLUGIN_ROOT).as_posix()
        for path in (
            PACKAGE_README_PATH,
            PACKAGE_LICENSE_PATH,
            PACKAGE_IGNORE_PATH,
            PLUGIN_MANIFEST_PATH,
            THIRD_PARTY_LICENSES_PATH,
            PLUGIN_ROOT / "assets" / "icon.svg",
        )
        if path.is_file()
    }

    assert present >= EXPECTED_PACKAGE_FILES
    assert (REPO_ROOT / "LICENSE").read_text(encoding="utf-8") == PACKAGE_LICENSE_PATH.read_text(
        encoding="utf-8"
    )
    ignore_text = PACKAGE_IGNORE_PATH.read_text(encoding="utf-8")
    assert ".git/" in ignore_text
    assert "build/" in ignore_text
    readme_text = PACKAGE_README_PATH.read_text(encoding="utf-8")
    assert "compatibility copy" in readme_text
    assert "Athena marketplace" in readme_text


def test_third_party_notice_covers_all_derived_skills() -> None:
    """The third-party notice must name every MIT-derived skill."""
    text = THIRD_PARTY_LICENSES_PATH.read_text(encoding="utf-8")
    for skill in EXPECTED_THIRD_PARTY_SKILLS:
        assert skill in text
    assert "MIT License" in text
    assert "Jesse Vincent" in text


def test_plugin_manifest_is_strictly_schema_valid() -> None:
    """The plugin manifest must match the strict marketplace schema."""
    manifest = _load_manifest()
    Draft202012Validator(PLUGIN_MANIFEST_SCHEMA).validate(manifest)

    assert set(manifest) == {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
        "skills",
        "interface",
    }
    assert set(manifest["author"]) == {"name", "url"}
    assert set(manifest["interface"]) == {
        "displayName",
        "shortDescription",
        "longDescription",
        "developerName",
        "category",
        "capabilities",
        "websiteURL",
        "defaultPrompt",
        "composerIcon",
    }
    assert manifest["skills"] == "./skills/"
    assert manifest["interface"]["composerIcon"] == "./assets/icon.svg"
    assert "screenshots" not in manifest
    assert "logo" not in manifest
    assert "termsOfServiceUrl" not in manifest["interface"]
    assert "privacyPolicyUrl" not in manifest["interface"]


def test_runtime_configuration_stays_on_athena() -> None:
    """The runtime marketplace must remain Athena-only."""
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    enabled = settings["enabledPlugins"]

    assert not any(name.rpartition("@")[2].casefold() == "hephaestus" for name in enabled)
    assert settings["extraKnownMarketplaces"] == {
        "Athena": {
            "source": {
                "source": "git",
                "url": "https://github.com/HomericIntelligence/Athena.git",
            }
        }
    }
