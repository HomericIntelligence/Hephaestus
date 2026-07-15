"""Guard the canonical contributor workflow documented by issue #2138."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

CANONICAL_LINKS = (
    "CONTRIBUTING.md#development-setup",
    "CONTRIBUTING.md#platform-support",
    "CONTRIBUTING.md#code-style",
    "CONTRIBUTING.md#testing",
    "CONTRIBUTING.md#dependency-updates",
    "CONTRIBUTING.md#pull-request-process",
)
REMOVED_README_HEADINGS = (
    "### Development setup",
    "## Getting Started with Pixi",
    "## Development Guidelines",
    "## Pixi Environments",
    "## Adding New Dependencies",
)
CONTRIBUTOR_COMMANDS = (
    "just bootstrap",
    "just check",
    "pixi run pytest tests/unit",
    "pixi run format",
    "pixi run lint",
    "git checkout -b",
    "git commit -S",
    "git push -u origin",
)


def test_contributing_is_canonical_and_readme_has_no_workflow_copy() -> None:
    """Contributor procedures must live only in CONTRIBUTING.md."""
    contributing = CONTRIBUTING.read_text(encoding="utf-8")
    readme = README.read_text(encoding="utf-8")

    assert "canonical source for contributor setup and workflow" in contributing.lower()
    for heading in REMOVED_README_HEADINGS:
        assert heading not in readme
    for command in CONTRIBUTOR_COMMANDS:
        assert command not in readme


def test_readme_cross_links_every_canonical_workflow_section() -> None:
    """README must route contributors to each relevant canonical section."""
    readme = README.read_text(encoding="utf-8")
    missing = [link for link in CANONICAL_LINKS if link not in readme]
    assert not missing, f"README.md is missing canonical contributor links: {missing}"


def test_contributing_platform_support_matches_pixi_declarations() -> None:
    """Canonical platform guidance must describe the supported Pixi environments."""
    contributing = CONTRIBUTING.read_text(encoding="utf-8")

    assert "`osx-arm64`" in contributing
    assert "3.10–3.13" in contributing
    assert "`linux-64` only" not in contributing
