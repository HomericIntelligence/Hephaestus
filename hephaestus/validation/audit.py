"""Filter pip-audit JSON output to fail only on HIGH/CRITICAL severity vulnerabilities.

Reads pip-audit JSON from stdin, classifies vulnerabilities by CVSS v3 base score,
and exits non-zero only for HIGH (7.0+) or CRITICAL (9.0+) findings. Lower-severity
findings are reported as warnings. Supports an ignore list via ``.pip-audit-ignore.txt``.

Usage::

    pip-audit --format json | hephaestus-filter-audit
    pip-audit --format json | hephaestus-filter-audit --ignore-file .pip-audit-ignore.txt
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from hephaestus.utils.helpers import get_repo_root

HIGH_THRESHOLD: float = 7.0

CVSS_PATTERN = re.compile(r"CVSS:\d+\.\d+/.*")


def load_ignore_list(path: Path | None = None) -> frozenset[str]:
    """Load the set of ignored vulnerability IDs from an ignore file.

    Lines starting with ``#`` or empty lines are ignored.

    Args:
        path: Path to the ignore file. If None, looks for
            ``.pip-audit-ignore.txt`` in the repo root. Returns empty set
            if file does not exist.

    Returns:
        Frozenset of ignored vulnerability IDs (e.g. ``"GHSA-xxx-yyy-zzz"``).

    """
    if path is None:
        try:
            path = get_repo_root() / ".pip-audit-ignore.txt"
        except (FileNotFoundError, RuntimeError):
            return frozenset()

    if not path.exists():
        return frozenset()

    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.split("#")[0].strip()
        if stripped:
            ids.append(stripped)
    return frozenset(ids)


def extract_cvss_score(severity_list: list[dict[str, Any]]) -> float | None:
    """Extract the highest CVSS base score from a severity list.

    Args:
        severity_list: List of severity entries from pip-audit JSON output.

    Returns:
        Highest CVSS score found, or None if no numeric score is available.

    """
    scores: list[float] = []
    for entry in severity_list:
        score_str = entry.get("score", "")
        if isinstance(score_str, (int, float)):
            scores.append(float(score_str))
        elif isinstance(score_str, str) and CVSS_PATTERN.match(score_str):
            pass
        numeric = entry.get("base_score") or entry.get("cvss_score")
        if numeric is not None:
            with contextlib.suppress(TypeError, ValueError):
                scores.append(float(numeric))
    return max(scores) if scores else None


def severity_label(score: float | None) -> str:
    """Return a human-readable severity label from a CVSS score.

    Args:
        score: CVSS v3 base score (0.0-10.0), or None.

    Returns:
        One of ``"CRITICAL"``, ``"HIGH"``, ``"MEDIUM"``, ``"LOW"``,
        ``"NONE"``, or ``"UNKNOWN"``.

    """
    if score is None:
        return "UNKNOWN"
    if score >= 9.0:
        return "CRITICAL"
    if score >= 7.0:
        return "HIGH"
    if score >= 4.0:
        return "MEDIUM"
    if score >= 0.1:
        return "LOW"
    return "NONE"


AuditEntry = tuple[str, str, str, str]  # (package, version, vuln_id, label)


def filter_audit_results(
    data: dict[str, Any],
    ignore_ids: frozenset[str] = frozenset(),
    threshold: float = HIGH_THRESHOLD,
) -> tuple[list[AuditEntry], list[AuditEntry]]:
    """Filter pip-audit JSON results by severity.

    Args:
        data: Parsed pip-audit JSON output.
        ignore_ids: Set of vulnerability IDs to skip.
        threshold: CVSS score at or above which vulnerabilities block CI.

    Returns:
        Tuple of ``(blocking, suppressed)`` where each is a list of
        ``(package, version, vuln_id, severity_label)`` tuples.

    """
    blocking: list[AuditEntry] = []
    suppressed: list[AuditEntry] = []

    for dep in data.get("dependencies", []):
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []):
            vuln_id = vuln.get("id", "?")
            if vuln_id in ignore_ids:
                continue
            severity_list = vuln.get("severity", [])
            score = extract_cvss_score(severity_list)
            label = severity_label(score)
            entry: AuditEntry = (name, version, vuln_id, label)
            if score is not None and score >= threshold:
                blocking.append(entry)
            else:
                suppressed.append(entry)

    return blocking, suppressed


def main() -> int:
    """Parse pip-audit JSON from stdin and exit non-zero on HIGH/CRITICAL findings.

    Returns:
        Exit code (0 if no blocking vulnerabilities, 1 otherwise).

    """
    parser = _build_parser()
    args = parser.parse_args()

    ignore_ids = load_ignore_list(args.ignore_file)
    if ignore_ids:
        print(f"pip-audit: ignoring {len(ignore_ids)} advisory ID(s)")

    raw = sys.stdin.read()
    json_start = raw.find("{")
    if json_start == -1:
        print("pip-audit: no vulnerabilities found", file=sys.stderr)
        return 0

    try:
        data = json.loads(raw[json_start:])
    except json.JSONDecodeError as exc:
        print(f"filter_audit: failed to parse pip-audit JSON: {exc}", file=sys.stderr)
        return 1

    blocking, suppressed = filter_audit_results(data, ignore_ids)

    if suppressed:
        print("pip-audit: suppressed vulnerabilities (LOW/MEDIUM/UNKNOWN — not blocking CI):")
        for name, version, vuln_id, label in suppressed:
            print(f"  [{label}] {name}=={version} {vuln_id}")

    if blocking:
        print("pip-audit: BLOCKING vulnerabilities found (HIGH/CRITICAL):")
        for name, version, vuln_id, label in blocking:
            print(f"  [{label}] {name}=={version} {vuln_id}")
        return 1

    if not suppressed:
        print("pip-audit: no vulnerabilities found")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    """Build argument parser for the filter-audit CLI."""

    parser = argparse.ArgumentParser(
        description="Filter pip-audit JSON to fail only on HIGH/CRITICAL vulnerabilities",
        epilog="Usage: pip-audit --format json | %(prog)s",
    )
    parser.add_argument(
        "--ignore-file",
        type=Path,
        default=None,
        help="Path to ignore file (default: .pip-audit-ignore.txt in repo root)",
    )
    return parser


if __name__ == "__main__":
    sys.exit(main())
