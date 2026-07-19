"""Regression test: ROADMAP.md must define an explicit iteration cadence.

Issue #1493 (S10 Planning, MODULARITY): the roadmap previously said it was
"reviewed and updated at the end of each release cycle (typically monthly)"
without defining the trigger, whether releases are date- or feature-driven,
or who owns the review. This guard fails if that section regresses to vague
prose. It asserts the HARD invariant (required phrases are present), never an
unverifiable cadence value like "monthly".
"""

import datetime
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ROADMAP_MD = REPO_ROOT / "docs" / "ROADMAP.md"

_SECTION_RE = re.compile(
    r"^##\s+Updating This Roadmap\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)

_CURRENT_FOCUS_RE = re.compile(r"^##\s+Current Focus \(Q([1-4]) (\d{4})\)\s*$", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(r"^Last updated: (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


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


def _stated_period() -> tuple[int, int]:
    """Return (year, quarter) parsed from the Current Focus heading, failing loud."""
    text = ROADMAP_MD.read_text(encoding="utf-8")
    match = _CURRENT_FOCUS_RE.search(text)
    assert match is not None, (
        "ROADMAP.md must have a '## Current Focus (Q<n> <year>)' heading so the "
        "current-period guard can parse it (issue #2158); restore the heading "
        "or update _CURRENT_FOCUS_RE."
    )
    return int(match.group(2)), int(match.group(1))


def test_current_focus_period_has_not_ended() -> None:
    """Fail once the quarter named in 'Current Focus' is in the past (issue #2158)."""
    today = datetime.date.today()
    current = (today.year, (today.month - 1) // 3 + 1)
    stated = _stated_period()
    assert stated >= current, (
        f"ROADMAP.md 'Current Focus' still names Q{stated[1]} {stated[0]}, but the "
        f"current period is Q{current[1]} {current[0]}. Refresh the Current Focus "
        "section per '## Updating This Roadmap' and bump the 'Last updated' line."
    )


def test_last_updated_not_before_stated_quarter() -> None:
    """Fail if 'Last updated' predates the stated quarter (half-refreshed roadmap)."""
    text = ROADMAP_MD.read_text(encoding="utf-8")
    match = _LAST_UPDATED_RE.search(text)
    assert match is not None, (
        "ROADMAP.md must end with a 'Last updated: YYYY-MM-DD' line; restore it "
        "or update _LAST_UPDATED_RE."
    )
    last_updated = datetime.date.fromisoformat(match.group(1))
    year, quarter = _stated_period()
    quarter_start = datetime.date(year, 3 * quarter - 2, 1)
    assert last_updated >= quarter_start, (
        f"ROADMAP.md says Current Focus is Q{quarter} {year} but 'Last updated' is "
        f"{last_updated}, before that quarter began ({quarter_start}); refresh both together."
    )
