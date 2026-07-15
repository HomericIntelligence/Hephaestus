"""Regression test: ROADMAP.md must define an explicit iteration cadence.

Issue #1493 (S10 Planning, MODULARITY): the roadmap previously said it was
"reviewed and updated at the end of each release cycle (typically monthly)"
without defining the trigger, whether releases are date- or feature-driven,
or who owns the review. This guard fails if that section regresses to vague
prose. It asserts the HARD invariant (required phrases are present), never an
unverifiable cadence value like "monthly".
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ROADMAP_MD = REPO_ROOT / "docs" / "ROADMAP.md"
RELEASING_MD = REPO_ROOT / "docs" / "RELEASING.md"

_SECTION_RE = re.compile(
    r"^##\s+Updating This Roadmap\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _updating_section() -> str:
    text = ROADMAP_MD.read_text(encoding="utf-8")
    match = _SECTION_RE.search(text)
    assert match is not None, (
        "ROADMAP.md no longer has an '## Updating This Roadmap' section; "
        "restore it or update this test's heading regex."
    )
    return match.group(1)


def test_cadence_is_release_driven_not_vague_monthly() -> None:
    """Fail if the roadmap's cadence section lacks the explicit trigger/driver/owner."""
    # Collapse whitespace so phrase checks are robust to line wrapping in the
    # source markdown (e.g. "auto tag\nrelease" wraps across two lines).
    section = re.sub(r"\s+", " ", _updating_section().lower())
    # The vague phrasing this issue removed must not come back.
    assert "typically monthly" not in section, (
        "ROADMAP.md reverted to the vague 'typically monthly' cadence "
        "(issue #1493); state the release-driven trigger explicitly instead."
    )
    # Trigger: tied to the Auto Tag Release pipeline, not a calendar.
    assert "auto tag release" in section, (
        "The cadence section must reference the 'Auto Tag Release' workflow "
        "as the release-cycle trigger (see docs/RELEASING.md)."
    )
    # Driver: explicitly feature/fix-driven, not date-driven.
    assert "not date-driven" in section, (
        "The cadence section must state releases are feature/fix-driven, not date-driven."
    )
    # Responsible party must be named.
    assert "maintainer" in section, (
        "The cadence section must name who is responsible for the review "
        "(the maintainer cutting the release)."
    )


def test_release_checklist_owns_roadmap_refresh() -> None:
    """The release maintainer's checklist must include the roadmap review."""
    checklist = RELEASING_MD.read_text(encoding="utf-8").lower()

    assert "docs/roadmap.md" in checklist
    assert "open epics" in checklist
    assert "audit-finding" in checklist
