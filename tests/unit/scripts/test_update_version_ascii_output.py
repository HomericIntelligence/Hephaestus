"""Regression test: scripts/update_version.py emits ASCII-only status markers.

Guards against re-introduction of emoji (✅/❌) in operator output —
see GitHub issue #769.
"""

from __future__ import annotations

import subprocess
import sys

from tests.unit.scripts.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "update_version.py"


def _run_verify(version: str) -> subprocess.CompletedProcess[bytes]:
    """Run update_version.py --verify-only and capture output."""
    return subprocess.run(
        [sys.executable, str(SCRIPT), version, "--verify-only"],
        capture_output=True,
        cwd=REPO_ROOT,
        timeout=30,
        check=False,
    )


def test_verify_output_is_ascii_only() -> None:
    """update_version.py --verify-only must emit only ASCII bytes."""
    result = _run_verify("0.0.0")
    combined = result.stdout + result.stderr
    # Emoji ✅ = U+2705 (\xe2\x9c\x85), ❌ = U+274C (\xe2\x9d\x8c).
    # Reject any non-ASCII byte to catch the whole class.
    assert all(b < 0x80 for b in combined), (
        f"non-ASCII bytes in update_version.py output: {combined!r}"
    )


def test_verify_uses_expected_ascii_markers() -> None:
    """Output must use [OK] or [FAIL] markers (the repo's established style)."""
    result = _run_verify("0.0.0")
    combined = (result.stdout + result.stderr).decode("ascii")
    assert "[OK]" in combined or "[FAIL]" in combined, (
        f"expected [OK] or [FAIL] marker in output, got: {combined!r}"
    )
