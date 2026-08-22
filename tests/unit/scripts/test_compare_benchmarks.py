"""Behavior tests for the benchmark comparison command."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.unit.scripts.conftest import REPO_ROOT

SCRIPT = REPO_ROOT / "scripts" / "compare_benchmarks.py"


def _run_compare(tmp_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    """Run the benchmark comparison script against a critical regression."""
    baseline = tmp_path / "baseline.json"
    current = tmp_path / "current.json"
    baseline.write_text(
        json.dumps({"benchmarks": [{"name": "example", "duration_ms": 100}]}),
        encoding="utf-8",
    )
    current.write_text(
        json.dumps({"benchmarks": [{"name": "example", "duration_ms": 130}]}),
        encoding="utf-8",
    )

    return subprocess.run(
        [sys.executable, str(SCRIPT), str(current), str(baseline), *extra_args],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )


def test_critical_regression_does_not_fail_by_default(tmp_path: Path) -> None:
    """Critical regressions are report-only unless the flag is supplied."""
    result = _run_compare(tmp_path)

    assert result.returncode == 0
    assert "1 critical" in result.stderr
    assert "failure disabled" in result.stderr


def test_fail_on_regression_returns_nonzero(tmp_path: Path) -> None:
    """The explicit failure flag makes a critical regression fatal."""
    result = _run_compare(tmp_path, "--fail-on-regression")

    assert result.returncode == 1
    assert "FAIL: 1 critical regressions detected" in result.stderr


def test_help_documents_failure_is_opt_in() -> None:
    """CLI help describes the report-only default for the failure flag."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "--fail-on-regression" in result.stdout
    assert "default: report only" in result.stdout
