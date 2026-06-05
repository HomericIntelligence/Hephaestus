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


def test_readme_flags_pixi_as_linux_only() -> None:
    """README must warn that the pixi dev env is Linux-only before `pixi install`."""
    text = README.read_text(encoding="utf-8")
    pixi_section_start = text.index("## Getting Started with Pixi")
    pixi_install_pos = text.index("pixi install", pixi_section_start)
    preface = text[pixi_section_start:pixi_install_pos]
    assert "linux-64" in preface.lower() or "linux only" in preface.lower(), (
        "README must mention the linux-64 restriction before the first "
        "`pixi install` command so off-Linux users do not hit a hard failure."
    )
