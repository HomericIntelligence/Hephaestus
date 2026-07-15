"""Regression test for README ↔ CONTRIBUTING platform-support cross-reference.

Issue #767: install/upgrade docs for non-Linux platforms must point readers
from the README (the first-landed surface) to the canonical comparison table
in CONTRIBUTING.md. This test guards both halves of that link.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "README.md"
CONTRIBUTING = REPO_ROOT / "CONTRIBUTING.md"

# Markdown anchor GitHub generates for "### Platform Support".
PLATFORM_SUPPORT_ANCHOR = "CONTRIBUTING.md#platform-support"


def test_contributing_has_platform_support_heading() -> None:
    """The canonical Platform Support table must exist in CONTRIBUTING.md."""
    text = CONTRIBUTING.read_text(encoding="utf-8")
    assert "### Platform Support" in text, (
        "CONTRIBUTING.md must contain a '### Platform Support' heading; README links to its anchor."
    )


def test_readme_links_to_platform_support_section() -> None:
    """README must cross-link to the CONTRIBUTING Platform Support anchor."""
    text = README.read_text(encoding="utf-8")
    assert PLATFORM_SUPPORT_ANCHOR in text, (
        f"README.md must link to {PLATFORM_SUPPORT_ANCHOR} so macOS/Windows "
        "readers find the supported install path before running `pixi install`."
    )


def test_readme_routes_platform_support_to_contributing() -> None:
    """README must route contributor platform details to the canonical guide."""
    text = README.read_text(encoding="utf-8")
    contributing_start = text.find("## Contributing")
    assert contributing_start != -1, "README must contain a '## Contributing' section"
    assert text.find(PLATFORM_SUPPORT_ANCHOR, contributing_start) != -1, (
        "README's Contributing section must link to the canonical Platform Support section"
    )
