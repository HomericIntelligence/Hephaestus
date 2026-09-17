#!/usr/bin/env python3
"""Summarize pytest JUnit testcase durations by test module."""

from __future__ import annotations

import argparse
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path
from typing import Any


def module_path(class_name: str) -> str:
    """Convert a pytest JUnit class name to a repository test-module path."""
    parts = class_name.split(".")
    if len(parts) < 2 or parts[0] != "tests" or not all(parts):
        raise ValueError(f"invalid pytest JUnit class name: {class_name!r}")
    module_index = max(
        (index for index, part in enumerate(parts) if part.startswith("test_")),
        default=0,
    )
    if module_index == 0:
        raise ValueError(f"invalid pytest JUnit class name: {class_name!r}")
    return "/".join(parts[: module_index + 1]) + ".py"


def summarize(junit_path: Path) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic totals for each module in one JUnit report."""
    try:
        root = ET.parse(junit_path).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"cannot read JUnit report {junit_path}: {error}") from error

    totals: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"seconds": 0.0, "tests": 0, "failures": 0}
    )
    for testcase in root.iter("testcase"):
        class_name = testcase.get("classname")
        duration = testcase.get("time")
        if class_name is None or duration is None:
            raise ValueError("JUnit testcase lacks classname or time")
        try:
            seconds = float(duration)
        except ValueError as error:
            raise ValueError(f"JUnit testcase has invalid time: {duration!r}") from error
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"JUnit testcase has invalid time: {duration!r}")
        record = totals[module_path(class_name)]
        record["seconds"] += seconds
        record["tests"] += 1
        record["failures"] += int(
            testcase.find("failure") is not None or testcase.find("error") is not None
        )

    return {
        "modules": [
            {
                "module": module,
                "seconds": round(float(record["seconds"]), 6),
                "tests": int(record["tests"]),
                "failures": int(record["failures"]),
            }
            for module, record in sorted(
                totals.items(), key=lambda item: (item[0].count("/"), item[0])
            )
        ]
    }


def main(arguments: list[str] | None = None) -> int:
    """Write one JSON module-duration summary from one JUnit report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    parsed.output.write_text(
        json.dumps(summarize(parsed.junit), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        raise SystemExit(str(error)) from error
