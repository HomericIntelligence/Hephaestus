"""Run the immutable-review unit coverage gate without a shell wrapper."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from hephaestus.utils.helpers import run_subprocess

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
    """Run one fixed Python-module command and preserve its captured output."""
    result = run_subprocess([sys.executable, *args], check=False)
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def main() -> int:
    """Generate unit coverage, then enforce the repository coverage policy."""
    test_rc = _run(_UNIT_COVERAGE_ARGS)
    if test_rc != 0:
        return test_rc
    return _run(_COVERAGE_POLICY_ARGS)


if __name__ == "__main__":
    raise SystemExit(main())
