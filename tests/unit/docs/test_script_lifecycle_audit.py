"""Guard: the lifecycle audit accounts for every tracked script."""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AUDIT = REPO_ROOT / "docs" / "SCRIPT_LIFECYCLE_AUDIT.md"


def _tracked_scripts() -> list[str]:
    """Return tracked paths under ``scripts/``."""
    output = subprocess.run(
        ["git", "ls-files", "scripts/"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in output.splitlines() if line)


def test_lifecycle_audit_accounts_for_every_tracked_script() -> None:
    """Every tracked script has a disposition in the lifecycle audit."""
    audit = AUDIT.read_text(encoding="utf-8")
    scripts = _tracked_scripts()
    assert scripts, "git ls-files returned no scripts — audit guard cannot see its inputs"
    missing = [script for script in scripts if f"`{script}`" not in audit]
    assert not missing, "Lifecycle audit is missing entries for: " + ", ".join(missing)
