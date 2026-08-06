"""Filter pip-audit JSON output using a fail-closed severity policy.

Reads pip-audit JSON from stdin, classifies vulnerabilities by CVSS v3 base score,
and exits non-zero for HIGH (7.0+), CRITICAL (9.0+), or unscored findings. Lower-
severity findings are reported as warnings. Supports an ignore list via
``.pip-audit-ignore.txt``.

Usage::

    pip-audit --format json | hephaestus-filter-audit
    pip-audit --format json | hephaestus-filter-audit --ignore-file .pip-audit-ignore.txt
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, cast

from hephaestus.cli.utils import create_validation_parser, emit_json_status, format_output
from hephaestus.utils.helpers import get_repo_root

HIGH_THRESHOLD: float = 7.0
_CVSS_V3_METRICS: frozenset[str] = frozenset({"AV", "AC", "PR", "UI", "S", "C", "I", "A"})
_CVSS_V3_IMPACT_WEIGHT: dict[str, float] = {"H": 0.56, "L": 0.22, "N": 0.0}
_CVSS_V3_BASE_WEIGHTS: dict[str, dict[str, float]] = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2},
    "AC": {"L": 0.77, "H": 0.44},
    "UI": {"N": 0.85, "R": 0.62},
}
_CVSS_V3_PR_WEIGHTS: dict[str, dict[str, float]] = {
    "U": {"N": 0.85, "L": 0.62, "H": 0.27},
    "C": {"N": 0.85, "L": 0.68, "H": 0.5},
}


def _parse_cvss_numeric_score(value: object) -> float | None:
    """Return a finite CVSS score within the inclusive 0.0-10.0 range."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not 0.0 <= parsed <= 10.0:
        return None
    return parsed


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
        score = entry.get("score", "")
        if isinstance(score, (int, float)):
            parsed = _parse_cvss_numeric_score(score)
            if parsed is not None:
                scores.append(parsed)
        elif isinstance(score, str):
            parsed = _parse_cvss_numeric_score(score)
            if parsed is not None:
                scores.append(parsed)
            else:
                # A string is either a numeric score or a CVSS vector, never
                # both — only try vector scoring when numeric parse failed.
                vector_score = _score_cvss_v3_vector(score)
                if vector_score is not None:
                    scores.append(vector_score)
        for field in ("base_score", "cvss_score"):
            parsed = _parse_cvss_numeric_score(entry.get(field))
            if parsed is not None:
                scores.append(parsed)
    return max(scores) if scores else None


def _score_cvss_v3_vector(vector: str) -> float | None:
    stripped = vector.strip().upper()
    if not stripped.startswith(("CVSS:3.0/", "CVSS:3.1/")):
        return None

    _, *metric_parts = stripped.split("/")
    metrics: dict[str, str] = {}
    for part in metric_parts:
        key, separator, value = part.partition(":")
        if not separator or not key or not value:
            return None
        metrics[key] = value

    if not _CVSS_V3_METRICS.issubset(metrics):
        return None

    scope = metrics["S"]
    if scope not in _CVSS_V3_PR_WEIGHTS:
        return None

    try:
        impact_subscore = 1 - (
            (1 - _CVSS_V3_IMPACT_WEIGHT[metrics["C"]])
            * (1 - _CVSS_V3_IMPACT_WEIGHT[metrics["I"]])
            * (1 - _CVSS_V3_IMPACT_WEIGHT[metrics["A"]])
        )
        impact = _cvss_v3_impact(scope, impact_subscore)
        exploitability = (
            8.22
            * _CVSS_V3_BASE_WEIGHTS["AV"][metrics["AV"]]
            * _CVSS_V3_BASE_WEIGHTS["AC"][metrics["AC"]]
            * _CVSS_V3_PR_WEIGHTS[scope][metrics["PR"]]
            * _CVSS_V3_BASE_WEIGHTS["UI"][metrics["UI"]]
        )
    except KeyError:
        return None

    if impact <= 0:
        return 0.0

    raw_score = impact + exploitability
    if scope == "C":
        raw_score *= 1.08
    return _cvss_v3_round_up(min(raw_score, 10.0))


def _cvss_v3_impact(scope: str, impact_subscore: float) -> float:
    # The changed-scope Impact formula is identical in CVSS 3.0 and 3.1
    # (the 0.9731/^13 form belongs to the v3.1 Environmental ModifiedImpact,
    # not the base score).
    if scope == "U":
        return 6.42 * impact_subscore
    return 7.52 * (impact_subscore - 0.029) - 3.25 * (impact_subscore - 0.02) ** 15


def _cvss_v3_round_up(value: float) -> float:
    return math.ceil(value * 10 - 1e-7) / 10


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


def _validate_audit_vulnerability(vulnerability: object, path: str) -> None:
    """Validate one vulnerability record and its optional severity list."""
    if not isinstance(vulnerability, dict):
        raise ValueError(f"{path} must be an object")

    vulnerability_id = vulnerability.get("id")
    if not isinstance(vulnerability_id, str) or not vulnerability_id.strip():
        raise ValueError(f"{path}.id must be a nonempty string")

    severity = vulnerability.get("severity", [])
    if not isinstance(severity, list):
        raise ValueError(f"{path}.severity must be a list")
    if any(not isinstance(entry, dict) for entry in severity):
        raise ValueError(f"{path}.severity entries must be objects")


def _validate_audit_dependency(dependency: object, index: int) -> None:
    """Validate one dependency record and all of its vulnerabilities."""
    path = f"dependencies[{index}]"
    if not isinstance(dependency, dict):
        raise ValueError(f"{path} must be an object")

    for field in ("name", "version"):
        value = dependency.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}.{field} must be a nonempty string")

    vulnerabilities = dependency.get("vulns")
    if not isinstance(vulnerabilities, list):
        raise ValueError(f"{path}.vulns must be a list")

    for vulnerability_index, vulnerability in enumerate(vulnerabilities):
        vuln_path = f"{path}.vulns[{vulnerability_index}]"
        _validate_audit_vulnerability(vulnerability, vuln_path)


def _validate_audit_result(data: object) -> dict[str, Any]:
    """Validate the pip-audit result shape consumed by this filter."""
    if not isinstance(data, dict):
        raise ValueError("expected a top-level object")

    dependencies = data.get("dependencies")
    if not isinstance(dependencies, list):
        raise ValueError("dependencies must be a list")

    for dependency_index, dependency in enumerate(dependencies):
        _validate_audit_dependency(dependency, dependency_index)

    return cast(dict[str, Any], data)


def filter_audit_results(
    data: object,
    ignore_ids: frozenset[str] = frozenset(),
    threshold: float = HIGH_THRESHOLD,
) -> tuple[list[AuditEntry], list[AuditEntry]]:
    """Filter validated pip-audit results using a fail-closed severity policy.

    Args:
        data: Parsed pip-audit JSON output.
        ignore_ids: Set of vulnerability IDs to skip.
        threshold: CVSS score at or above which vulnerabilities block CI.

    Returns:
        Tuple of ``(blocking, suppressed)`` where each is a list of
        ``(package, version, vuln_id, severity_label)`` tuples.

    Raises:
        ValueError: If data is not a complete scanner-result structure.

    """
    validated = _validate_audit_result(data)
    blocking: list[AuditEntry] = []
    suppressed: list[AuditEntry] = []

    for dependency in validated["dependencies"]:
        name = dependency["name"]
        version = dependency["version"]
        for vulnerability in dependency["vulns"]:
            vulnerability_id = vulnerability["id"]
            if vulnerability_id in ignore_ids:
                continue

            score = extract_cvss_score(vulnerability.get("severity", []))
            entry: AuditEntry = (
                name,
                version,
                vulnerability_id,
                severity_label(score),
            )
            if score is None or score >= threshold:
                blocking.append(entry)
            else:
                suppressed.append(entry)

    return blocking, suppressed


def main() -> int:
    """Parse pip-audit JSON and exit non-zero on blocking findings.

    Returns:
        Exit code (0 if no blocking vulnerabilities, 1 otherwise).

    """
    parser = _build_parser()
    args = parser.parse_args()

    try:
        data = _parse_audit_input(sys.stdin.read())
    except ValueError as exc:
        return _audit_input_error(str(exc), args.json)

    ignore_ids = load_ignore_list(args.ignore_file)
    if ignore_ids and not args.json:
        print(f"pip-audit: ignoring {len(ignore_ids)} advisory ID(s)")

    blocking, suppressed = filter_audit_results(data, ignore_ids)

    if args.json:
        return _emit_audit_json(blocking, suppressed)

    if suppressed:
        print("pip-audit: non-blocking vulnerabilities (below configured threshold):")
        for name, version, vuln_id, label in suppressed:
            print(f"  [{label}] {name}=={version} {vuln_id}")

    if blocking:
        print("pip-audit: BLOCKING vulnerabilities found (HIGH/CRITICAL/UNKNOWN):")
        for name, version, vuln_id, label in blocking:
            print(f"  [{label}] {name}=={version} {vuln_id}")
        return 1

    if not suppressed:
        print("pip-audit: no vulnerabilities found")
    return 0


def _audit_input_error(detail: str, json_mode: bool) -> int:
    """Emit a fail-closed scanner-evidence error in the requested mode."""
    message = f"invalid pip-audit evidence: {detail}"
    if json_mode:
        emit_json_status(1, message=message)
    else:
        print(f"filter_audit: {message}", file=sys.stderr)
    return 1


def _parse_audit_input(raw: str) -> dict[str, Any]:
    """Decode and validate one complete pip-audit JSON document."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse JSON: {exc}") from exc
    return _validate_audit_result(parsed)


def _emit_audit_json(blocking: list[AuditEntry], suppressed: list[AuditEntry]) -> int:
    """Emit the audit findings as a JSON report and return the exit code."""
    report = {
        "blocking": [
            {"package": n, "version": v, "id": vid, "severity": lbl} for n, v, vid, lbl in blocking
        ],
        "suppressed": [
            {"package": n, "version": v, "id": vid, "severity": lbl}
            for n, v, vid, lbl in suppressed
        ],
        "exit_code": 1 if blocking else 0,
    }
    print(format_output(report, "json"))
    return 1 if blocking else 0


def _build_parser() -> argparse.ArgumentParser:
    """Build argument parser for the filter-audit CLI."""
    parser = create_validation_parser(
        "Validate pip-audit JSON and fail on HIGH, CRITICAL, or unscored advisories",
        include_repo_root=False,
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
