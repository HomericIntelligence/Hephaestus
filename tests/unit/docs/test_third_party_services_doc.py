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
    "OpenAI",
    "Pi private provider",
    "npm",
    "Dependabot",
    "Renovate",
)

# First-party GitHub actions (owner ``actions``) are covered by the GitHub
# inventory row rather than listed individually.
FIRST_PARTY_ACTION_OWNERS = frozenset({"actions"})


def _documented_action_owners(repo_root: Path = REPO_ROOT) -> set[str]:
    """Remote ``uses:`` owners referenced by workflows and composite actions.

    The trailing ``@`` in the pattern restricts matches to remote pinned
    actions (``owner/repo@ref``), excluding local composite actions referenced
    as ``uses: ./.github/actions/...``.
    """
    owners: set[str] = set()
    for definitions in (
        repo_root / ".github" / "workflows",
        repo_root / ".github" / "actions",
    ):
        for pattern in ("*.yml", "*.yaml"):
            for definition in definitions.rglob(pattern):
                text = definition.read_text(encoding="utf-8")
                for match in re.finditer(
                    r"^\s*-\s*uses:\s*([\w.-]+)/[\w./-]+@", text, re.MULTILINE
                ):
                    owners.add(match.group(1))
    return owners


def _inventory_table_rows() -> list[list[str]]:
    """Parse the ``## Service inventory`` markdown table into cell rows.

    Returns the header row followed by every data row, each as a list of
    trimmed cell strings. Raises if the section or its table is missing, so a
    heading/format rename fails loudly instead of silently matching nothing.
    """
    text = DOC.read_text(encoding="utf-8")
    section = re.search(r"^## Service inventory\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert section is not None, "docs/third-party-services.md has no '## Service inventory' section"
    lines = [line for line in section.group(1).splitlines() if line.strip().startswith("|")]
    assert len(lines) >= 2, "no markdown table found under '## Service inventory'"
    # Row 1 is the header; row 2 is the '---' separator; skip it.
    data_lines = [line for line in lines if not re.fullmatch(r"[\s|:-]+", line)]
    rows = [line.strip().strip("|").split("|") for line in data_lines]
    return [[cell.strip() for cell in row] for row in rows]


def test_inventory_names_every_required_service() -> None:
    """Every required third-party service must have its own inventory row."""
    rows = _inventory_table_rows()
    service_cells = [row[0] for row in rows[1:]]
    missing = [
        service
        for service in REQUIRED_SERVICES
        if not any(service in cell for cell in service_cells)
    ]
    assert missing == [], f"docs/third-party-services.md missing service rows: {missing}"


def test_inventory_has_responsibility_and_status_columns() -> None:
    """The inventory table header must split responsibility and cite a status column."""
    header = _inventory_table_rows()[0]
    assert any("our responsibility" in cell.lower() for cell in header), header
    assert any("vendor responsibility" in cell.lower() for cell in header), header
    assert any("status" in cell.lower() for cell in header), header


def test_every_third_party_action_owner_is_documented() -> None:
    """A new external CI vendor must be added to the inventory table."""
    owners = _documented_action_owners() - FIRST_PARTY_ACTION_OWNERS
    assert owners, "no remote action owners found — regex or workflow layout changed"
    service_cells = [row[0].lower() for row in _inventory_table_rows()[1:]]
    missing = sorted(
        owner for owner in owners if not any(owner.lower() in cell for cell in service_cells)
    )
    assert missing == [], f"CI action owners absent from docs/third-party-services.md: {missing}"


def test_composite_action_owner_is_discovered(tmp_path: Path) -> None:
    """A nested composite action cannot bypass the external-owner guard."""
    action = tmp_path / ".github" / "actions" / "bootstrap" / "action.yml"
    action.parent.mkdir(parents=True)
    action.write_text(
        "runs:\n  using: composite\n  steps:\n"
        "    - uses: example/setup-tool@0123456789abcdef0123456789abcdef01234567\n",
        encoding="utf-8",
    )

    assert _documented_action_owners(tmp_path) == {"example"}


def test_doc_is_linked_from_index() -> None:
    """The inventory must be discoverable from the docs index."""
    text = INDEX.read_text(encoding="utf-8")
    assert "third-party-services.md" in text
