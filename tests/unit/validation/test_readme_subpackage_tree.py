"""Behavioral checks for README navigation links."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.markdown import extract_markdown_links, validate_file_links

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"


def test_readme_exposes_navigation_links() -> None:
    """The README contains discoverable Markdown navigation links."""
    links = extract_markdown_links(README.read_text(encoding="utf-8"))

    assert links


def test_readme_navigation_targets_resolve_inside_repository() -> None:
    """README local links resolve through the shared Markdown validator."""
    result = validate_file_links(README, REPO_ROOT)

    assert result["broken_links"] == []
