"""The enumerable ADR contract stays complete (ADR-0068 included)."""

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


# A number stays reserved while the PR that claims it is still open. ADR-0067
# is the pr-bound-repair-publication record on PR #3636; this branch allocates
# ADR-0068 so both changes can land without a number race.
_RESERVED_NUMBERS = {67}


def test_adr_numbers_are_contiguous_and_unique() -> None:
    """ADR numbers stay unique and contiguous from 0001 minus reserved gaps."""
    files = _adr_files()
    assert files
    numbers = set(files)
    expected = set(range(1, max(numbers) + 1)) - _RESERVED_NUMBERS
    assert numbers == expected


def test_adr_0068_plan_decomposition_is_in_the_index() -> None:
    """ADR-0068 is enumerable in the ADR index with its decision title."""
    files = _adr_files()
    assert 68 in files
    adr = files[68].read_text(encoding="utf-8")
    index = (_ADR_DIR / "README.md").read_text(encoding="utf-8")
    assert adr.startswith("# ADR-0068: Plan decomposition into single-PR epics")
    assert "[0068](0068-plan-decomposition.md)" in index
    assert "Plan decomposition into single-PR epics" in index


def test_adr_0068_records_the_required_decomposition_decisions() -> None:
    """The ADR records every decision the issue plan requires."""
    adr = _adr_files()[68].read_text(encoding="utf-8")
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
