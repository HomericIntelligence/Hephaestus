"""Filter pip-audit JSON output using a fail-closed severity policy.

The module has two modes:

* ``--scan`` runs the pinned pip-audit command, applies the structured
  suppression ledger at the repository root, and returns the canonical exit
  contract.
* Stdin mode classifies pip-audit JSON that a caller already produced. It
  applies no suppression.

Exit codes: ``0`` means the scan found no unsuppressed blocking finding, ``1``
means it found an unsuppressed HIGH, CRITICAL, or unscored finding, and ``2``
means the scan cannot produce a verdict.

Usage::

    uv run hephaestus-filter-audit --scan
    pip-audit --format json | hephaestus-filter-audit
"""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, NamedTuple, cast

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from hephaestus.cli.localization import text
from hephaestus.cli.utils import create_validation_parser, emit_json_status, format_output
from hephaestus.errors import boundary_main
from hephaestus.utils.helpers import get_repo_root

HIGH_THRESHOLD: float = 7.0
OPERATIONAL_ERROR_EXIT: int = 2
LEDGER_FILENAME: str = ".pip-audit-ignore.txt"

_LEDGER_FIELDS: tuple[str, ...] = (
    "advisory",
    "package",
    "version",
    "owner",
    "review",
    "expires",
    "rationale",
)
_LEDGER_SEPARATOR = " | "
_RATIONALE_MAX_CHARS = 200
_ADVISORY_PATTERN = re.compile(r"[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9][A-Za-z0-9._]*)+")
_PACKAGE_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_OWNER_PATTERN = re.compile(r"@[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")
_REVIEW_PATTERN = re.compile(r"(?:issue|pr):#[1-9][0-9]*")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_AUDIT_COMMAND: tuple[str, ...] = (
    sys.executable,
    "-m",
    "pip_audit",
    "--format",
    "json",
    "--progress-spinner",
    "off",
)
_AUDIT_TIMEOUT_SECONDS = 300
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

SuppressionKey = tuple[str, str, str]  # (advisory, canonical package, canonical version)


class _LedgerRecord(NamedTuple):
    """One reviewed suppression record from the canonical ledger."""

    advisory: str
    package: str
    version: str
    owner: str
    review: str
    expires: date
    rationale: str

    @property
    def key(self) -> SuppressionKey:
        """Return the normalized finding key this record suppresses."""
        return (self.advisory, self.package, self.version)


class _Finding(NamedTuple):
    """One scanner finding and the verdict it carries."""

    key: SuppressionKey
    entry: tuple[str, str, str, str]
    score: float | None


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


def _has_control_characters(value: str) -> bool:
    """Return True when the value carries an ASCII control character."""
    return any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)


def _utc_today() -> date:
    """Return the current date in UTC."""
    return datetime.now(UTC).date()


def _validate_advisory(value: str) -> str:
    if not _ADVISORY_PATTERN.fullmatch(value):
        raise ValueError("advisory must be an uppercase advisory identifier")
    return value


def _validate_package(value: str) -> str:
    if not _PACKAGE_PATTERN.fullmatch(value) or canonicalize_name(value) != value:
        raise ValueError("package must be a PEP 503 normalized name")
    return value


def _validate_version(value: str) -> str:
    try:
        canonical = str(Version(value))
    except InvalidVersion as exc:
        raise ValueError("version must be a PEP 440 version") from exc
    if canonical != value:
        raise ValueError("version must be its canonical PEP 440 rendering")
    return canonical


def _validate_owner(value: str) -> str:
    if not _OWNER_PATTERN.fullmatch(value):
        raise ValueError("owner must be a GitHub login with a leading '@'")
    return value


def _validate_review(value: str) -> str:
    if not _REVIEW_PATTERN.fullmatch(value):
        raise ValueError("review must be 'issue:#N' or 'pr:#N'")
    return value


def _validate_expires(value: str, today: date) -> date:
    if not _DATE_PATTERN.fullmatch(value):
        raise ValueError("expires must be an ISO date")
    try:
        expires = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("expires must be an ISO date") from exc
    if expires < today:
        raise ValueError("expires must not be in the past")
    return expires


def _validate_rationale(value: str) -> str:
    if not value or len(value) > _RATIONALE_MAX_CHARS:
        raise ValueError(f"rationale must contain 1 to {_RATIONALE_MAX_CHARS} characters")
    return value


def _parse_ledger_record(line: str, *, today: date) -> _LedgerRecord:
    """Parse one physical ledger line into a validated suppression record."""
    parts = line.split(_LEDGER_SEPARATOR)
    if len(parts) != len(_LEDGER_FIELDS):
        raise ValueError(
            f"expected {len(_LEDGER_FIELDS)} fields separated by ' | ', found {len(parts)}"
        )

    values: dict[str, str] = {}
    for field, part in zip(_LEDGER_FIELDS, parts, strict=True):
        name, separator, value = part.partition("=")
        if not separator:
            raise ValueError(f"field {field!r} must use 'name=value'")
        if name != field:
            raise ValueError(f"expected field {field!r}, found {name!r}")
        if value != value.strip():
            raise ValueError(f"field {field!r} has leading or trailing whitespace")
        if "|" in value:
            raise ValueError(f"field {field!r} must not contain '|'")
        if _has_control_characters(value):
            raise ValueError(f"field {field!r} must not contain control characters")
        values[field] = value

    return _LedgerRecord(
        advisory=_validate_advisory(values["advisory"]),
        package=_validate_package(values["package"]),
        version=_validate_version(values["version"]),
        owner=_validate_owner(values["owner"]),
        review=_validate_review(values["review"]),
        expires=_validate_expires(values["expires"], today),
        rationale=_validate_rationale(values["rationale"]),
    )


def _parse_ledger(
    content: str,
    *,
    today: date,
    source: str = LEDGER_FILENAME,
) -> tuple[_LedgerRecord, ...]:
    """Parse the complete suppression ledger, one record per physical line."""
    records: list[_LedgerRecord] = []
    seen: set[SuppressionKey] = set()
    for lineno, line in enumerate(content.splitlines(), start=1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            record = _parse_ledger_record(line, today=today)
        except ValueError as exc:
            raise ValueError(f"{source}:{lineno}: {exc}") from exc
        if record.key in seen:
            raise ValueError(
                f"{source}:{lineno}: duplicate record for "
                f"{record.advisory} {record.package}=={record.version}"
            )
        seen.add(record.key)
        records.append(record)
    return tuple(records)


def _resolve_ledger_path(root: Path | None = None) -> Path:
    """Return the canonical ledger path or raise when it is not the exact file."""
    repo_root = get_repo_root() if root is None else Path(root).resolve()
    ledger = repo_root / LEDGER_FILENAME
    if not ledger.is_file():
        raise ValueError(f"suppression ledger {ledger} does not exist")
    if ledger.is_symlink():
        raise ValueError(f"suppression ledger {ledger} must not be a symlink")
    if ledger.resolve() != ledger:
        raise ValueError(f"suppression ledger must resolve to {ledger}")
    return ledger


def _load_suppression_ledger(
    path: Path,
    *,
    today: date | None = None,
) -> tuple[_LedgerRecord, ...]:
    """Read and validate the structured suppression ledger."""
    return _parse_ledger(path.read_text(encoding="utf-8"), today=today or _utc_today())


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

    name = dependency.get("name")
    if not isinstance(name, str) or canonicalize_name(name) != name or not name:
        raise ValueError(f"{path}.name must be a PEP 503 normalized name")

    if "skip_reason" in dependency:
        # The scanner reports a dependency it cannot audit with a skip reason
        # and no version. It carries no advisory evidence, so it cannot block.
        skip_reason = dependency["skip_reason"]
        if not isinstance(skip_reason, str) or not skip_reason.strip():
            raise ValueError(f"{path}.skip_reason must be a nonempty string")
        if "vulns" in dependency:
            raise ValueError(f"{path} cannot be both skipped and audited")
        return

    version = dependency.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{path}.version must be a nonempty string")
    try:
        Version(version)
    except InvalidVersion as exc:
        raise ValueError(f"{path}.version must be a PEP 440 version") from exc

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

    fixes = data.get("fixes")
    if not isinstance(fixes, list):
        raise ValueError("fixes must be a list")

    for dependency_index, dependency in enumerate(dependencies):
        _validate_audit_dependency(dependency, dependency_index)

    return cast(dict[str, Any], data)


def _skipped_dependencies(validated: dict[str, Any]) -> list[str]:
    """Return the names of dependencies the scanner could not audit."""
    return [
        dependency["name"]
        for dependency in validated["dependencies"]
        if "skip_reason" in dependency
    ]


def _collect_findings(validated: dict[str, Any]) -> list[_Finding]:
    """Normalize scanner evidence into finding keys and verdict entries."""
    findings: list[_Finding] = []
    for dependency in validated["dependencies"]:
        if "vulns" not in dependency:
            continue
        name = dependency["name"]
        version = dependency["version"]
        key_version = str(Version(version))
        for vulnerability in dependency["vulns"]:
            advisory = vulnerability["id"]
            score = extract_cvss_score(vulnerability.get("severity", []))
            findings.append(
                _Finding(
                    key=(advisory, canonicalize_name(name), key_version),
                    entry=(name, version, advisory, severity_label(score)),
                    score=score,
                )
            )
    return findings


def _match_suppressions(
    findings: list[_Finding],
    suppressions: tuple[_LedgerRecord, ...],
) -> set[SuppressionKey]:
    """Return the keys that exactly one reviewed record suppresses."""
    counts = Counter(finding.key for finding in findings)
    approved: set[SuppressionKey] = set()
    for record in suppressions:
        matches = counts.get(record.key, 0)
        if matches == 0:
            raise ValueError(
                f"suppression record for {record.advisory} "
                f"{record.package}=={record.version} matches no current finding"
            )
        if matches > 1:
            raise ValueError(
                f"suppression record for {record.advisory} "
                f"{record.package}=={record.version} matches {matches} findings"
            )
        approved.add(record.key)
    return approved


def _classify_audit_results(
    data: object,
    *,
    suppressions: tuple[_LedgerRecord, ...] = (),
    threshold: float = HIGH_THRESHOLD,
) -> tuple[list[AuditEntry], list[AuditEntry], list[AuditEntry], list[str]]:
    """Split validated evidence into blocking, warning, approved, and skipped."""
    validated = _validate_audit_result(data)
    findings = _collect_findings(validated)
    approved_keys = _match_suppressions(findings, suppressions)

    blocking: list[AuditEntry] = []
    warnings: list[AuditEntry] = []
    approved: list[AuditEntry] = []
    for finding in findings:
        if finding.key in approved_keys:
            approved.append(finding.entry)
        elif finding.score is None or finding.score >= threshold:
            blocking.append(finding.entry)
        else:
            warnings.append(finding.entry)
    return blocking, warnings, approved, _skipped_dependencies(validated)


def filter_audit_results(
    data: object,
    threshold: float = HIGH_THRESHOLD,
) -> tuple[list[AuditEntry], list[AuditEntry]]:
    """Filter validated pip-audit results using a fail-closed severity policy.

    The filter classifies evidence only. Suppression belongs to the canonical
    ``--scan`` path, which owns the ledger.

    Args:
        data: Parsed pip-audit JSON output.
        threshold: CVSS score at or above which vulnerabilities block CI.

    Returns:
        Tuple of ``(blocking, warnings)`` where each is a list of
        ``(package, version, vuln_id, severity_label)`` tuples.

    Raises:
        ValueError: If data is not a complete scanner-result structure.

    """
    blocking, warnings, _approved, _skipped = _classify_audit_results(data, threshold=threshold)
    return blocking, warnings


def _run_pip_audit() -> subprocess.CompletedProcess[str]:
    """Run the pinned scanner command and capture its JSON evidence."""
    return subprocess.run(
        list(_AUDIT_COMMAND),
        capture_output=True,
        text=True,
        check=False,
        timeout=_AUDIT_TIMEOUT_SECONDS,
    )


def _check_scan_status(returncode: int, validated: dict[str, Any]) -> None:
    """Cross-check the scanner status against the evidence it returned."""
    has_findings = any(dependency.get("vulns") for dependency in validated["dependencies"])
    if returncode not in (0, 1):
        raise ValueError(f"pip-audit exited with status {returncode}")
    if returncode == 0 and has_findings:
        raise ValueError("pip-audit exited 0 while reporting vulnerabilities")
    if returncode == 1 and not has_findings:
        raise ValueError("pip-audit exited 1 without reporting vulnerabilities")


def main() -> int:
    """Run hephaestus-filter-audit through the shared console-script boundary.

    The boundary reports a failure with one exit code and one stderr line.
    A usage error still exits 2 from argparse.
    """
    return boundary_main(_run)


def _run() -> int:
    """Run the requested audit mode and return its canonical exit code."""
    args = _build_parser().parse_args()

    if args.scan:
        return _run_scan(json_mode=args.json)

    try:
        data = _parse_audit_input(sys.stdin.read())
    except ValueError as exc:
        return _fail(f"invalid pip-audit evidence: {exc}", json_mode=args.json)

    try:
        blocking, warnings, _approved, skipped = _classify_audit_results(data)
    except ValueError as exc:
        return _fail(f"invalid pip-audit evidence: {exc}", json_mode=args.json)
    return _emit_report(blocking, warnings, [], skipped, json_mode=args.json)


def _run_scan(*, json_mode: bool) -> int:
    """Run the canonical scan and apply the validated suppression ledger."""
    try:
        ledger_path = _resolve_ledger_path()
        suppressions = _load_suppression_ledger(ledger_path)
    except (OSError, ValueError) as exc:
        return _fail(f"suppression ledger rejected: {exc}", json_mode=json_mode)

    try:
        completed = _run_pip_audit()
    except (OSError, subprocess.SubprocessError) as exc:
        return _fail(f"cannot run the canonical pip-audit command: {exc}", json_mode=json_mode)

    try:
        data = _parse_audit_input(completed.stdout)
        _check_scan_status(completed.returncode, data)
    except ValueError as exc:
        return _fail(f"invalid pip-audit evidence: {exc}", json_mode=json_mode)

    try:
        blocking, warnings, approved, skipped = _classify_audit_results(
            data, suppressions=suppressions
        )
    except ValueError as exc:
        return _fail(f"suppression ledger rejected: {exc}", json_mode=json_mode)

    return _emit_report(blocking, warnings, approved, skipped, json_mode=json_mode)


def _fail(message: str, *, json_mode: bool) -> int:
    """Report an operational failure and return the operational exit code."""
    if json_mode:
        emit_json_status(OPERATIONAL_ERROR_EXIT, message=message)
    else:
        print(text("filter_audit: %(message)s", message=message), file=sys.stderr)
    return OPERATIONAL_ERROR_EXIT


def _parse_audit_input(raw: str) -> dict[str, Any]:
    """Decode and validate one complete pip-audit JSON document."""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"failed to parse JSON: {exc}") from exc
    return _validate_audit_result(parsed)


def _entry_record(entry: AuditEntry) -> dict[str, str]:
    """Render one finding entry for the JSON report."""
    name, version, advisory, label = entry
    return {"package": name, "version": version, "id": advisory, "severity": label}


def _entry_line(entry: AuditEntry) -> str:
    """Render one finding entry for human output."""
    name, version, advisory, label = entry
    return text(
        "  [%(value0)s] %(value1)s==%(value2)s %(value3)s",
        value0=label,
        value1=name,
        value2=version,
        value3=advisory,
    )


def _emit_report(
    blocking: list[AuditEntry],
    warnings: list[AuditEntry],
    approved: list[AuditEntry],
    skipped: list[str],
    *,
    json_mode: bool,
) -> int:
    """Emit one audit verdict and return its exit code."""
    exit_code = 1 if blocking else 0
    if json_mode:
        print(
            format_output(
                {
                    "blocking": [_entry_record(entry) for entry in blocking],
                    "warnings": [_entry_record(entry) for entry in warnings],
                    "approved_suppressions": [_entry_record(entry) for entry in approved],
                    "skipped_dependencies": skipped,
                    "exit_code": exit_code,
                },
                "json",
            )
        )
        return exit_code

    sections = (
        ("pip-audit: approved suppressions:", approved),
        ("pip-audit: non-blocking vulnerabilities (below configured threshold):", warnings),
        ("pip-audit: BLOCKING vulnerabilities found (HIGH/CRITICAL/UNKNOWN):", blocking),
    )
    for heading, entries in sections:
        if not entries:
            continue
        print(text(heading))
        for entry in entries:
            print(_entry_line(entry))
    if skipped:
        print(text("pip-audit: dependencies the scanner could not audit:"))
        for name in skipped:
            print(text("  %(value0)s", value0=name))
    if not blocking and not warnings and not approved and not skipped:
        print(text("pip-audit: no vulnerabilities found"))
    return exit_code


def _build_parser() -> argparse.ArgumentParser:
    """Build argument parser for the filter-audit CLI."""
    parser = create_validation_parser(
        "Run pip-audit or classify its JSON against the fail-closed severity policy",
        include_repo_root=False,
        epilog=(
            "Usage: uv run hephaestus-filter-audit --scan\n"
            "       pip-audit --format json | %(prog)s"
        ),
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        help=text("Run pip-audit and apply the repository-root suppression ledger"),
    )
    return parser


if __name__ == "__main__":
    sys.exit(main())
