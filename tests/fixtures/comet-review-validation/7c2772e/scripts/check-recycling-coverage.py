#!/usr/bin/env python3
"""Check coverage for the complete rolling-recycling implementation."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path

SUPERVISOR_METHODS = {
    "_cancel_recycling_replacement",
    "advance_recycling",
    "advance_recycling_no_spare",
    "recycling_pool_reconcilable",
    "recycling_status",
}


def coverage_lines(record: dict[str, object]) -> tuple[set[int], set[int]]:
    """Return executed and measurable lines from one coverage record."""
    executed = set(record["executed_lines"])
    measurable = executed | set(record["missing_lines"])
    return executed, measurable


def main() -> int:
    """Check the package and supervisor state-machine coverage."""
    parser = argparse.ArgumentParser()
    parser.add_argument("coverage_json", type=Path)
    parser.add_argument("--fail-under", type=float, default=85.0)
    args = parser.parse_args()

    files = json.loads(args.coverage_json.read_text())["files"]
    executed_count = 0
    measurable_count = 0

    for name, record in files.items():
        if name.startswith("src/comet/recycling/"):
            executed, measurable = coverage_lines(record)
            executed_count += len(executed)
            measurable_count += len(measurable)

    supervisor_name = "src/comet/supervisor.py"
    supervisor_record = files.get(supervisor_name)
    if supervisor_record is None:
        raise SystemExit(f"coverage data does not contain {supervisor_name}")
    executed, measurable = coverage_lines(supervisor_record)
    tree = ast.parse(Path(supervisor_name).read_text())
    method_lines: set[int] = set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in SUPERVISOR_METHODS:
            found.add(node.name)
            method_lines.update(range(node.lineno, node.end_lineno + 1))
    missing_methods = SUPERVISOR_METHODS - found
    if missing_methods:
        raise SystemExit(f"coverage method list is stale: {sorted(missing_methods)}")

    method_measurable = measurable & method_lines
    executed_count += len(executed & method_measurable)
    measurable_count += len(method_measurable)
    percent = 100.0 * executed_count / measurable_count
    print(f"rolling recycling line coverage: {percent:.2f}%")
    return 0 if percent >= args.fail_under else 1


if __name__ == "__main__":
    raise SystemExit(main())
