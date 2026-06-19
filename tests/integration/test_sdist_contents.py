#!/usr/bin/env python3
"""Integration test: sdist ships required top-level metadata files."""

from __future__ import annotations

import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRED_TOP_LEVEL_FILES = {
    "README.md",
    "LICENSE",
    "NOTICE",
    "COMPATIBILITY.md",
    "pyproject.toml",
}


@pytest.mark.integration
def test_sdist_includes_notice_and_compatibility(tmp_path: Path) -> None:
    """Building the sdist must include NOTICE and COMPATIBILITY.md (issue #765)."""
    probe = subprocess.run(
        [sys.executable, "-m", "build", "--help"],
        cwd=REPO_ROOT.parent,
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0 and b"No module named build" in probe.stderr:
        pytest.skip("python build frontend is not installed in this environment")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            str(REPO_ROOT),
            "--sdist",
            "--outdir",
            str(tmp_path),
        ],
        cwd=REPO_ROOT.parent,
        check=True,
        capture_output=True,
    )
    sdists = list(tmp_path.glob("*.tar.gz"))
    assert len(sdists) == 1, f"expected one sdist, got {sdists}"

    with tarfile.open(sdists[0], "r:gz") as tf:
        # sdist members are prefixed with "<name>-<version>/"; strip the prefix.
        top_level = {
            Path(m.name).parts[1]
            for m in tf.getmembers()
            if len(Path(m.name).parts) == 2 and m.isfile()
        }

    missing = REQUIRED_TOP_LEVEL_FILES - top_level
    assert not missing, f"sdist is missing required files: {sorted(missing)}"
    assert len(top_level) > 3, f"suspiciously few top-level files: {top_level}"
