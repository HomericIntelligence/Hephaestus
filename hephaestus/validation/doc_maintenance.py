"""Validate maintenance contracts for living normative documentation.

The validator is deliberately read-only. It rejects unsupported operational
snapshots and verifies that the documents with maintained state identify an
owner, update trigger, and versioned source.

Usage::

    uv run python -m hephaestus.validation.doc_maintenance --repo-root .
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

EXCLUDED_PREFIXES = (
    ".git/",
    ".pytest_cache/",
    ".venv/",
    ".worktrees/",
    "build/",
    "tests/fixtures/",
)
SOURCE_CONTRACTS = (
    ("docs/AUTOMATION_LOOP_ARCHITECTURE.md", "hephaestus/automation/pipeline/routing.py", "ROUTES"),
    ("docs/ci/required-checks.md", ".github/workflows/_required.yml", "jobs"),
    ("docs/ROADMAP.md", "docs/RELEASING.md", "Pre-Release Checklist"),
)
_TEMPORARY_STATE_RE = re.compile(r"\bcurrently\s+(?:inactive|active|open|closed)\b", re.IGNORECASE)
_ISSUE_STATE_RE = re.compile(r"\bissues?\s+#?\d+(?:\s+\w+){0,3}\s+(?:open|closed)\b", re.IGNORECASE)
_DATE_RE = re.compile(
    r"\b(?:as of|last updated|updated on)\s*:?\s*\d{4}-\d{2}-\d{2}\b", re.IGNORECASE
)
_METRIC_RE = re.compile(
    r"\b\d{1,3}(?:,\d{3})*\s+(?:lines? of code|loc|tests?|source files?|packages?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    """A documentation-maintenance contract violation."""

    path: str
    line: int
    rule: str
    message: str


@dataclass(frozen=True)
class SourceContract:
    """A document's maintained source and semantic selector."""

    document: str
    source: str
    selector: str


def _relative_path(path: Path, repo_root: Path) -> str:
    """Return a portable path relative to the repository where possible."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _is_historical(relative_path: str) -> bool:
    """Return whether a tracked Markdown path is a point-in-time record."""
    if relative_path.startswith("docs/adr/"):
        return Path(relative_path).name != "README.md"
    if relative_path.startswith("docs/release-notes/"):
        return Path(relative_path).name != "README.md"
    return False


def discover_normative_markdown(repo_root: Path) -> list[Path]:
    """Recursively return living normative Markdown files under *repo_root*."""
    documents: list[Path] = []
    for path in repo_root.rglob("*.md"):
        relative_path = _relative_path(path, repo_root)
        if any(relative_path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        if not _is_historical(relative_path):
            documents.append(path)
    return sorted(documents)


def _in_fenced_lines(content: str) -> set[int]:
    """Return line numbers contained in fenced Markdown code blocks."""
    fenced_lines: set[int] = set()
    in_fence = False
    for number, line in enumerate(content.splitlines(), start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            fenced_lines.add(number)
        elif in_fence:
            fenced_lines.add(number)
    return fenced_lines


def _allows_maintained_dates(content: str) -> bool:
    """Return whether a document declares the maintenance fields for a date."""
    return all(
        marker in content for marker in ("**Owner:**", "**Trigger:**", "**Maintained source:**")
    )


def validate_volatile_claims(path: Path, *, repo_root: Path) -> list[Finding]:
    """Find unsupported operational snapshots in a living Markdown document."""
    content = path.read_text(encoding="utf-8")
    fenced_lines = _in_fenced_lines(content)
    permits_dates = _allows_maintained_dates(content)
    patterns = (
        (
            "temporary-state",
            _TEMPORARY_STATE_RE,
            "temporary operational state requires a maintained source",
        ),
        (
            "temporary-issue-state",
            _ISSUE_STATE_RE,
            "issue state belongs in GitHub, not a normative snapshot",
        ),
        (
            "repository-snapshot-metric",
            _METRIC_RE,
            "repository-size metric requires a maintained source",
        ),
    )
    if not permits_dates:
        patterns += (
            ("unowned-date", _DATE_RE, "dated state requires ownership and a review trigger"),
        )

    findings: list[Finding] = []
    relative_path = _relative_path(path, repo_root)
    for line_number, line in enumerate(content.splitlines(), start=1):
        if line_number in fenced_lines:
            continue
        for rule, pattern, message in patterns:
            if pattern.search(line):
                findings.append(Finding(relative_path, line_number, rule, message))
    return findings


def _python_has_selector(source: Path, selector: str) -> bool:
    """Return whether a Python module defines a named top-level selector."""
    try:
        tree = ast.parse(source.read_text(encoding="utf-8"))
    except SyntaxError:
        return False
    for node in tree.body:
        if getattr(node, "name", None) == selector:
            return True
        targets = getattr(node, "targets", ())
        if isinstance(node, ast.AnnAssign):
            targets = (node.target,)
        if any(target.id == selector for target in targets if isinstance(target, ast.Name)):
            return True
    return False


def _source_has_selector(source: Path, selector: str) -> bool:
    """Check a semantic selector appropriate for the source file type."""
    content = source.read_text(encoding="utf-8")
    if source.suffix == ".py":
        return _python_has_selector(source, selector)
    if source.suffix in {".yaml", ".yml"}:
        return bool(re.search(rf"^{re.escape(selector)}\s*:", content, re.MULTILINE))
    if source.suffix == ".md":
        return bool(re.search(rf"^#+\s+{re.escape(selector)}\s*$", content, re.MULTILINE))
    return selector in content


def validate_source_contracts(
    repo_root: Path,
    *,
    contracts: tuple[SourceContract, ...] | None = None,
) -> list[Finding]:
    """Validate that maintained document sources and selectors still exist."""
    active_contracts = contracts or tuple(
        SourceContract(*contract) for contract in SOURCE_CONTRACTS
    )
    findings: list[Finding] = []
    for contract in active_contracts:
        document = repo_root / contract.document
        source = repo_root / contract.source
        if not document.is_file():
            findings.append(
                Finding(contract.document, 0, "missing-document", "contract document is missing")
            )
        if not source.is_file():
            findings.append(
                Finding(
                    contract.source, 0, "missing-maintained-source", "maintained source is missing"
                )
            )
        elif not _source_has_selector(source, contract.selector):
            findings.append(
                Finding(
                    contract.source,
                    0,
                    "missing-semantic-selector",
                    f"selector {contract.selector!r} is missing",
                )
            )
    return findings


def validate_roadmap_maintenance(repo_root: Path) -> list[Finding]:
    """Validate the roadmap's owner, release trigger, and maintained source."""
    roadmap = repo_root / "docs" / "ROADMAP.md"
    if not roadmap.is_file():
        return [Finding("docs/ROADMAP.md", 0, "missing-roadmap", "roadmap is missing")]

    content = roadmap.read_text(encoding="utf-8")
    findings: list[Finding] = []
    for marker, rule in (
        ("**Owner:**", "missing-roadmap-owner"),
        ("**Trigger:**", "missing-roadmap-trigger"),
        ("**Maintained source:**", "missing-roadmap-source"),
    ):
        if marker not in content:
            findings.append(
                Finding("docs/ROADMAP.md", 0, rule, "roadmap maintenance metadata is missing")
            )

    return findings


def validate_documentation(repo_root: Path) -> list[Finding]:
    """Run every read-only maintenance validation for the repository."""
    findings = [
        finding
        for document in discover_normative_markdown(repo_root)
        for finding in validate_volatile_claims(document, repo_root=repo_root)
    ]
    findings.extend(validate_source_contracts(repo_root))
    findings.extend(validate_roadmap_maintenance(repo_root))
    return findings


def main() -> int:
    """Run the documentation-maintenance CLI and return its exit status."""
    parser = create_validation_parser("Validate living normative documentation maintenance")
    args = parser.parse_args()
    findings = validate_documentation(resolve_repo_root(args))
    report = {"passed": not findings, "findings": [asdict(finding) for finding in findings]}
    if args.json:
        print(json.dumps(report, sort_keys=True))
    elif findings:
        for finding in findings:
            print(f"ERROR: {finding.path}:{finding.line}: {finding.rule}: {finding.message}")
    else:
        print("OK: normative documentation maintenance checks passed")
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
