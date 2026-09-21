#!/usr/bin/env python3
"""Validate a private Gitleaks report and write only permitted metadata."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from pathlib import Path

REPORT_LIMIT = 16 * 1024 * 1024
METADATA_LIMIT = 8 * 1024 * 1024
FINDING_LIMIT = 10_000


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> object:
    raise ValueError


def _finding(value: object) -> tuple[str, str, int, str]:
    if not isinstance(value, dict):
        raise ValueError
    rule = value.get("RuleID")
    path = value.get("File")
    line = value.get("StartLine")
    commit = value.get("Commit")
    if not isinstance(rule, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", rule) is None:
        raise ValueError
    if (
        not isinstance(path, str)
        or not 1 <= len(path) <= 4096
        or "\\" in path
        or re.match(r"[A-Za-z]:", path) is not None
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(character) < 32 or 127 <= ord(character) <= 159 for character in path)
    ):
        raise ValueError
    if type(line) is not int or not 1 <= line <= 2_147_483_647:
        raise ValueError
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError
    return commit, path, line, rule


def _project(report_path: str, scan_status: str) -> bytes:
    descriptor = os.open(report_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as report:
        information = os.fstat(report.fileno())
        if not stat.S_ISREG(information.st_mode) or information.st_size > REPORT_LIMIT:
            raise ValueError
        raw = report.read(REPORT_LIMIT + 1)
    if len(raw) > REPORT_LIMIT:
        raise ValueError
    findings = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    if not isinstance(findings, list) or len(findings) > FINDING_LIMIT:
        raise ValueError
    if scan_status not in {"0", "1"} or bool(findings) != (scan_status == "1"):
        raise ValueError
    records = sorted({_finding(finding) for finding in findings})
    metadata = [
        {"rule_id": rule, "path": path, "line": line, "commit": commit}
        for commit, path, line, rule in records
    ]
    encoded = (json.dumps(metadata, ensure_ascii=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > METADATA_LIMIT:
        raise ValueError
    return encoded


def main() -> int:
    """Keep raw reports and exception details out of public output."""
    if len(sys.argv) != 4:
        return 2
    try:
        encoded = _project(sys.argv[1], sys.argv[3])
        with Path(sys.argv[2]).open("xb") as output:
            output.write(encoded)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
