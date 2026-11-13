"""The enumerable ADR contract stays complete (ADR-0066 included)."""

from __future__ import annotations

import re
from pathlib import Path

import hephaestus

_ADR_DIR = Path(hephaestus.__file__).parent.parent / "docs" / "adr"


def _adr_files() -> dict[int, Path]:
    """Map every ADR number to its file."""
    files: dict[int, Path] = {}
    for path in _ADR_DIR.glob("*.md"):
        match = re.fullmatch(r"(\d{4})-[a-z0-9-]+\.md", path.name)
        if match is None:
            continue
        number = int(match.group(1))
        assert number not in files, f"duplicate ADR number {number}"
        files[number] = path
    return files


def test_adr_numbers_are_contiguous_and_unique() -> None:
    """ADR numbers stay unique and contiguous from 0001."""
    files = _adr_files()
    assert files
    assert sorted(files) == list(range(1, max(files) + 1))


def test_adr_0066_plan_decomposition_is_in_the_index() -> None:
    """ADR-0066 is enumerable in the ADR index with its decision title."""
    files = _adr_files()
    assert 66 in files
    adr = files[66].read_text(encoding="utf-8")
    index = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert adr.startswith("# ADR-0066: Plan decomposition into single-PR epics")
    assert "[0066](0066-plan-decomposition.md)" in index
    assert "Plan decomposition into single-PR epics" in index


def test_adr_0066_records_the_required_decomposition_decisions() -> None:
    """The ADR records every decision the issue plan requires."""
    adr = _adr_files()[66].read_text(encoding="utf-8")
    for decision in (
        "Requirements epoch",
        "Record protocol",
        "Transaction",
        "Same-run recursion rule",
        "Fail-closed recovery",
    ):
        assert f"**{decision}.**" in adr
    assert "independent review session" in adr
    assert "`GO` verdict" in adr
