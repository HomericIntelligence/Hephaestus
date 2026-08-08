"""Structural guard for the Architecture Decision Record directory.

Keeps ``docs/adr/`` an enumerable, well-formed record: every ADR follows the
Nygard section skeleton, the numeric prefixes are contiguous and unique, and the
``README.md`` index stays bidirectionally in sync with the files on disk.
"""

import re
from pathlib import Path

ADR_DIR = Path(__file__).resolve().parents[3] / "docs" / "adr"
FILENAME_RE = re.compile(r"^\d{4}-[a-z0-9-]+\.md$")
REQUIRED_SECTIONS = (
    "## Context",
    "## Decision",
    "## Alternatives considered",
    "## Consequences",
)


def _adr_files() -> list[Path]:
    """Return the ADR markdown files, excluding the index ``README.md``."""
    return sorted(p for p in ADR_DIR.glob("*.md") if p.name != "README.md")


def test_every_adr_filename_is_well_formed() -> None:
    """Every ADR file uses the numeric-slug markdown filename convention."""
    bad = [p.name for p in _adr_files() if not FILENAME_RE.match(p.name)]
    assert not bad, f"Malformed ADR filenames: {bad}"


def test_adr_numbers_are_contiguous_and_unique() -> None:
    """ADR numeric prefixes stay contiguous from 0001 with no duplicates."""
    nums = sorted(int(p.name[:4]) for p in _adr_files())
    assert nums == list(range(1, len(nums) + 1)), f"ADR numbers not contiguous from 1: {nums}"


def test_every_adr_has_required_sections() -> None:
    """Every ADR follows the documented section skeleton."""
    for p in _adr_files():
        text = p.read_text(encoding="utf-8")
        assert re.search(rf"^# ADR-{p.name[:4]}:", text, re.MULTILINE), f"{p.name} missing title"
        assert "- Status:" in text and "- Date:" in text, f"{p.name} missing Status/Date"
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{p.name} missing section {section!r}"


def test_readme_index_lists_every_adr() -> None:
    """The ADR README index and the files on disk stay bidirectionally in sync."""
    readme = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    linked = set(re.findall(r"\(([0-9]{4}-[a-z0-9-]+\.md)\)", readme))
    on_disk = {p.name for p in _adr_files()}
    missing = on_disk - linked
    stale = linked - on_disk
    assert linked == on_disk, f"README index out of sync: missing={missing}, stale={stale}"
