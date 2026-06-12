#!/usr/bin/env python3
"""Check SECURITY.md supported-versions table matches the latest git tag.

ProjectHephaestus uses hatch-vcs dynamic versioning (CLAUDE.md "Version
Management"). The canonical version is the most recent `vX.Y.Z` git tag,
not any pyproject.toml field. This hook fails if SECURITY.md's supported
row drifts away from that latest tag's X.Y minor series.

Policy enforced: exactly ONE supported (✅) X.Y.x row and exactly ONE EOL
(❌) "< X.Y" row, both anchored to the latest released minor. If the
project moves to a multi-series support policy, update SECURITY.md AND
relax this script's row-count check together.

NOTE: get_repo_root() is duplicated across scripts/check_python_version_consistency.py
and scripts/check_version_single_source.py. The reusable version at
hephaestus/utils/helpers.py:99 is intentionally not imported here because
pre-commit hooks run via raw `python3` (no `pixi run` wrapper) to avoid
forcing a pixi env build on every commit; importing from `hephaestus`
would require switching this hook to `pixi run --environment default`.
A future consolidation PR can extract these into a stdlib helper.

Usage:
    python3 scripts/check_security_version_consistency.py
"""

import re
import subprocess
import sys
from pathlib import Path

GIT_TAG_CMD = ["tag", "--list", "v[0-9]*.*", "--sort=-v:refname"]


def get_repo_root() -> Path:
    """Return the repo root directory (where pyproject.toml exists)."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def latest_release_minor(repo_root: Path) -> str | None:
    """Return the X.Y of the most recent vX.Y.Z tag, or None if no tags."""
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

    latest = latest_release_minor(repo_root)
    if latest is None:
        print("WARNING: no vX.Y.Z tags found — skipping SECURITY.md drift check")
        return 0

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
    if supported_xy == latest and eol_xy == latest:
        print(f"OK: SECURITY.md supported = {supported_xy}.x matches latest tag v{latest}.*")
        return 0

    print("ERROR: SECURITY.md supported-versions table is out of sync with git tags")
    print(f"  latest released minor: {latest} (from `git tag --list 'v[0-9]*.*'`)")
    print(f"  SECURITY.md supported row: {supported_xy}.x")
    print(f"  SECURITY.md EOL threshold: < {eol_xy}")
    print(f"  fix: update SECURITY.md to show '{latest}.x' supported, '< {latest}' EOL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
