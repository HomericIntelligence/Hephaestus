"""Run the immutable-review unit coverage gate without a shell wrapper."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from hephaestus.config.child_environments import build_python_phase_env
from hephaestus.utils.helpers import run_subprocess

_FAILURE_OUTPUT_TAIL_CHARS = 12_000
_FAILURE_SUMMARY_MAX_LINES = 50
_UNIT_COVERAGE_ARGS: tuple[str, ...] = (
    "-m",
    "pytest",
    "tests/unit",
    "--override-ini=addopts=",
    "-v",
    "--strict-markers",
    "-m",
    "not nightly",
    "--cov=hephaestus",
    "--cov-report=term-missing",
    "--cov-report=xml",
)
_COVERAGE_POLICY_ARGS: tuple[str, ...] = (
    "-m",
    "hephaestus.validation.coverage",
    "--coverage-file",
    "coverage.xml",
    "--config",
    "coverage.toml",
)


def _run(args: Sequence[str]) -> int:
    """Run one fixed command, emitting bounded diagnostics only on failure."""
    result = run_subprocess(
        [sys.executable, *args], env=build_python_phase_env(Path.cwd()), check=False
    )
    if result.returncode != 0:
        summary = tuple(
            line for line in result.stdout.splitlines() if line.startswith(("FAILED ", "ERROR "))
        )[-_FAILURE_SUMMARY_MAX_LINES:]
        if result.stdout:
            print(result.stdout[-_FAILURE_OUTPUT_TAIL_CHARS:], end="")
        if result.stderr:
            print(result.stderr[-_FAILURE_OUTPUT_TAIL_CHARS:], end="", file=sys.stderr)
        if summary:
            print("\nHost coverage failure index:", file=sys.stderr)
            print("\n".join(summary), file=sys.stderr)
    return result.returncode


def main() -> int:
    """Generate unit coverage, then enforce the repository coverage policy."""
    test_rc = _run(_UNIT_COVERAGE_ARGS)
    if test_rc != 0:
        return test_rc
    return _run(_COVERAGE_POLICY_ARGS)


if __name__ == "__main__":
    raise SystemExit(main())
