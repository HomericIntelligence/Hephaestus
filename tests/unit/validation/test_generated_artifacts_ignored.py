"""Repository contracts for generated artifacts that must stay untracked."""

from __future__ import annotations

import subprocess
from pathlib import Path


def test_root_coverage_xml_is_ignored() -> None:
    """The coverage report cannot be accidentally staged by automation."""
    repository = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", "coverage.xml"],
        cwd=repository,
        check=False,
    )
    assert result.returncode == 0
