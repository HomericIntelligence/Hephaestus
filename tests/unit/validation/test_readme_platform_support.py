"""Regression tests for the README ↔ CONTRIBUTING platform-support link.

Issue #767 established CONTRIBUTING.md as the canonical comparison table.
Issue #2135 moves manifest-parity assertions into
test_pixi_platform_documentation.py while this file preserves the cross-link.
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
        f"README.md must link to {PLATFORM_SUPPORT_ANCHOR} so contributors "
        "can choose the correct Pixi or pip installation path."
    )
