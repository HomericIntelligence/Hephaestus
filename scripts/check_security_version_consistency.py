#!/usr/bin/env python3
"""Check SECURITY.md supported-versions table matches the latest git tag.

Hephaestus uses hatch-vcs dynamic versioning (CLAUDE.md "Version
Management"). The canonical version is the most recent `vX.Y.Z` git tag,
not any pyproject.toml field. This hook fails if SECURITY.md's supported
row drifts away from that latest tag's X.Y minor series.

Policy enforced: exactly ONE supported (✅) X.Y.x row and exactly ONE EOL
(❌) "< X.Y" row, both anchored to the latest released minor. If the
project moves to a multi-series support policy, update SECURITY.md AND
relax this script's row-count check together.

Grace period: the release workflow does not update SECURITY.md, so a
freshly-pushed tag turns every subsequent commit red until a human bumps
the table by hand. Rather than block on that unavoidable lag, a tag less
than GRACE_PERIOD_HOURS old is not yet treated as "latest" for this
check — the previous tag's minor is used instead, giving the maintainer
a window to land the SECURITY.md bump before the guard starts enforcing
the new minor.

NOTE: get_repo_root() logic is also implemented in
hephaestus.scripts_lib.check_version_single_source and
hephaestus.validation.python_version. The reusable version at
hephaestus/utils/helpers.py:99 is intentionally not imported here because
this pre-commit hook runs via raw `python3` (no `uv run` wrapper) to avoid
forcing a uv env build on every commit.

Usage:
    python3 scripts/check_security_version_consistency.py
"""

import re
import subprocess
import sys
import time
from pathlib import Path

GIT_TAG_CMD = ["tag", "--list", "v[0-9]*.*", "--sort=-v:refname"]
GRACE_PERIOD_HOURS = 48


def get_repo_root() -> Path:
    """Return the repo root directory (where pyproject.toml exists)."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def _tag_age_hours(repo_root: Path, tag: str) -> float | None:
    """Return the age in hours of the commit ``tag`` points at, or None on error.

    Uses the commit date (``%ct``), not the tag-object creation date, so it
    behaves the same for lightweight and annotated tags.
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), "log", "-1", "--format=%ct", tag],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        tag_epoch = int(result.stdout.strip())
    except ValueError:
        return None
    return (time.time() - tag_epoch) / 3600


def true_latest_release_minor(repo_root: Path) -> str | None:
    """Return the X.Y of the most recent vX.Y.Z tag, ignoring the grace period."""
    result = subprocess.run(
        ["git", "-C", str(repo_root), *GIT_TAG_CMD],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        m = re.match(r"^v(\d+)\.(\d+)\.\d+$", line.strip())
        if m:
            return f"{m.group(1)}.{m.group(2)}"
    return None


def latest_release_minor(
    repo_root: Path, *, grace_period_hours: float = GRACE_PERIOD_HOURS
) -> str | None:
    """Return the X.Y of the oldest-acceptable vX.Y.Z tag minor.

    This is the FLOOR of what SECURITY.md must show, not necessarily the
    true latest tag: a tag younger than ``grace_period_hours`` is skipped
    in favor of the next-oldest tag, so a maintainer has a window to land
    the SECURITY.md bump before the guard starts requiring the new minor.
    A SECURITY.md that already shows the true latest minor (bumped early,
    ahead of this floor) is accepted separately by ``main()`` via
    ``true_latest_release_minor`` — this function only ever makes the
    requirement more lenient, never stricter than the true latest tag.
    Returns None if no tags exist at all, or if every tag is within the
    grace period (nothing to enforce yet).
    """
    result = subprocess.run(
        ["git", "-C", str(repo_root), *GIT_TAG_CMD],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        tag = line.strip()
        m = re.match(r"^v(\d+)\.(\d+)\.\d+$", tag)
        if not m:
            continue
        age_hours = _tag_age_hours(repo_root, tag)
        if age_hours is not None and age_hours < grace_period_hours:
            continue
        return f"{m.group(1)}.{m.group(2)}"
    return None


def extract_table_rows(content: str) -> tuple[list[str], list[str]]:
    """Return (supported_xy_list, eol_threshold_xy_list) from SECURITY.md table.

    Returns ALL matching rows so the caller can enforce the
    exactly-one-supported, exactly-one-EOL policy.
    """
    supported = re.findall(r"^\|\s*(\d+\.\d+)\.x\s*\|\s*✅[^|]*\|\s*$", content, re.MULTILINE)
    eol = re.findall(r"^\|\s*<\s*(\d+\.\d+)\s*\|\s*❌[^|]*\|\s*$", content, re.MULTILINE)
    return supported, eol


def main() -> int:
    """Check SECURITY.md version table matches latest git tag."""
    repo_root = get_repo_root()
    security_path = repo_root / "SECURITY.md"
    if not security_path.exists():
        print(f"ERROR: SECURITY.md not found at {security_path}")
        return 1

    floor = latest_release_minor(repo_root)
    true_latest = true_latest_release_minor(repo_root)
    if floor is None:
        print(
            "WARNING: no vX.Y.Z tag old enough to enforce (none exist, or the "
            f"latest is within the {GRACE_PERIOD_HOURS}h grace period) — "
            "skipping SECURITY.md drift check"
        )
        return 0

    # Accept either the grace-period floor or the true latest minor: a
    # maintainer who bumps SECURITY.md the moment a tag is pushed (ahead of
    # the floor) must not be penalized for being early.
    acceptable = {v for v in (floor, true_latest) if v is not None}

    supported, eol = extract_table_rows(security_path.read_text())

    if len(supported) != 1:
        print(
            f"ERROR: SECURITY.md must contain exactly ONE supported (✅) row; "
            f"found {len(supported)}: {supported}"
        )
        print(
            "  If the policy is changing to multi-series support, update both "
            "SECURITY.md and the row-count check in this script together."
        )
        return 1
    if len(eol) != 1:
        print(
            f"ERROR: SECURITY.md must contain exactly ONE EOL (❌ '< X.Y') row; "
            f"found {len(eol)}: {eol}"
        )
        return 1

    supported_xy, eol_xy = supported[0], eol[0]
    if supported_xy == eol_xy and supported_xy in acceptable:
        print(f"OK: SECURITY.md supported = {supported_xy}.x matches an acceptable release minor")
        return 0

    print("ERROR: SECURITY.md supported-versions table is out of sync with git tags")
    print(f"  acceptable release minor(s): {sorted(acceptable)} (from `git tag --list`)")
    print(f"  SECURITY.md supported row: {supported_xy}.x")
    print(f"  SECURITY.md EOL threshold: < {eol_xy}")
    print(f"  fix: update SECURITY.md to show '{true_latest}.x' supported, '< {true_latest}' EOL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
