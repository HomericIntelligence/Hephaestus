"""Regression tests for repository-local skill topology documentation (#2134)."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_GUIDES = (REPO_ROOT / "AGENTS.md",)
TOPOLOGY_FILES = (
    *SKILL_GUIDES,
    REPO_ROOT / ".markdownlint.yaml",
)

LOCAL_SKILLS_PATH = re.compile(r"(?<![\w.-])(?:\./|Hephaestus/)?skills(?=[/\\`)])")


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
