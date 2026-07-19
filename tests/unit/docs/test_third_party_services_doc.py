"""Tests for the third-party service responsibility inventory (issue #2177)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC = REPO_ROOT / "docs" / "third-party-services.md"
INDEX = REPO_ROOT / "docs" / "index.md"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

REQUIRED_SERVICES = (
    "GitHub",
    "PyPI",
    "Anthropic",
    "Pi private provider",
    "npm",
    "Dependabot",
    "Renovate",
)

# First-party GitHub actions (owner ``actions``) are covered by the GitHub
# inventory row rather than listed individually.
FIRST_PARTY_ACTION_OWNERS = frozenset({"actions"})


def _documented_action_owners() -> set[str]:
    """Remote ``uses:`` owners referenced across ``.github/workflows/*.yml``.

    The trailing ``@`` in the pattern restricts matches to remote pinned
    actions (``owner/repo@ref``), excluding local composite actions referenced
    as ``uses: ./.github/actions/...``.
    """
    owners: set[str] = set()
    for workflow in WORKFLOWS.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for match in re.finditer(r"^\s*uses:\s*([\w.-]+)/[\w./-]+@", text, re.MULTILINE):
            owners.add(match.group(1))
    return owners


def test_inventory_names_every_required_service() -> None:
    """Every required third-party service must appear in the inventory."""
    text = DOC.read_text(encoding="utf-8")
    missing = [service for service in REQUIRED_SERVICES if service not in text]
    assert missing == [], f"docs/third-party-services.md missing services: {missing}"


def test_inventory_has_responsibility_and_status_columns() -> None:
    """The inventory must split responsibility and cite status pages."""
    text = DOC.read_text(encoding="utf-8")
    assert "Our responsibility" in text
    assert "Vendor responsibility" in text
    assert "status page" in text.lower()


def test_every_third_party_action_owner_is_documented() -> None:
    """A new external CI vendor must be added to the inventory table."""
    text = DOC.read_text(encoding="utf-8")
    owners = _documented_action_owners() - FIRST_PARTY_ACTION_OWNERS
    assert owners, "no remote action owners found — regex or workflow layout changed"
    missing = sorted(owner for owner in owners if owner not in text)
    assert missing == [], f"CI action owners absent from docs/third-party-services.md: {missing}"


def test_doc_is_linked_from_index() -> None:
    """The inventory must be discoverable from the docs index."""
    text = INDEX.read_text(encoding="utf-8")
    assert "third-party-services.md" in text
