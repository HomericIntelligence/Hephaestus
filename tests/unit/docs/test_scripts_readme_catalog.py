"""Guard: scripts/README.md catalogs every tracked script (issue #2168)."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
README = REPO_ROOT / "scripts" / "README.md"


def _tracked_scripts() -> list[str]:
    """Paths under scripts/, relative to scripts/, excluding the README."""
    out = subprocess.run(
        ["git", "ls-files", "scripts/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(
        line.removeprefix("scripts/")
        for line in out.splitlines()
        if line and line != "scripts/README.md"
    )


def test_every_tracked_script_is_documented() -> None:
    """Every tracked file under scripts/ has a catalog bullet in the README."""
    readme = README.read_text(encoding="utf-8")
    scripts = _tracked_scripts()
    assert scripts, "git ls-files returned no scripts — guard cannot see its inputs"
    missing = [s for s in scripts if f"`{s}`" not in readme]
    assert not missing, (
        "scripts/README.md 'Available Scripts' is missing entries for: "
        + ", ".join(missing)
        + ". Add a one-line catalog bullet for each (see issue #2168)."
    )
