"""Validate ownership and currency contracts for living documentation.

The validator is deliberately offline and read-only.  It checks Markdown
content, local source links, and a small set of semantic source selectors so
that normative documentation can point at maintained implementation sources
without embedding transient repository snapshots.

Usage::

    python -m hephaestus.validation.doc_maintenance --repo-root .
    python -m hephaestus.validation.doc_maintenance --repo-root . --json
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from urllib.parse import unquote

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root
from hephaestus.scripts_lib.check_cli_table_sync import (
    _load_scripts,
    check_prose_counts,
)


class Severity(str, Enum):
    """Severity levels reported by the documentation validator."""

    ERROR = "ERROR"


@dataclass(frozen=True)
class Finding:
    """A documentation maintenance violation."""

    file: str
    line: int
    rule: str
    description: str
    content: str = ""
    severity: Severity = Severity.ERROR

    def as_dict(self) -> dict[str, str | int]:
        """Return this finding in a JSON-serialisable form."""
        return {
            "file": self.file,
            "line": self.line,
            "content": self.content.strip(),
            "rule": self.rule,
            "severity": self.severity.value,
            "description": self.description,
        }


@dataclass(frozen=True)
class SourceContract:
    """Describe a normative document's maintained source and selector."""

    document: str
    source: str
    selector: str


# These prefixes describe generated, temporary, or checkout-internal trees.
# Keep the explicit tests/fixtures/ entry: fixture documents must not become
# normative merely because they are nested below a documentation directory.
EXCLUDED_PREFIXES: tuple[str, ...] = (
    ".git/",
    ".pytest_cache/",
    ".venv/",
    ".worktrees/",
    "build/",
    "tests/fixtures/",
    "docs/api/",
    "docs/arxiv/",
    "tests/claude-code/",
    "node_modules/",
)

_HISTORICAL_PREFIXES: tuple[str, ...] = ("docs/adr/", "docs/release-notes/")
_HISTORICAL_INDEXES = frozenset({"docs/adr/README.md", "docs/release-notes/README.md"})

SOURCE_CONTRACTS: tuple[SourceContract, ...] = (
    SourceContract(
        document="docs/architecture.md",
        source="hephaestus/automation/pipeline/routing.py",
        selector="ROUTES",
    ),
    SourceContract(
        document="docs/specs/2026-07-16-jinja-prompt-templates-design.md",
        source="hephaestus/prompts/catalog.py",
        selector="PromptCatalog",
    ),
    SourceContract(
        document="docs/ci/required-checks.md",
        source=".github/workflows/_required.yml",
        selector="jobs",
    ),
    SourceContract(
        document="docs/ROADMAP.md",
        source="docs/RELEASING.md",
        selector="Pre-Release Checklist",
    ),
)

_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+[^)]*)?\)")
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_CURRENT_FOCUS_RE = re.compile(r"^##\s+Current Focus \(Q([1-4]) (\d{4})\)\s*$", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(
    r"^Last updated:\s*(\d{4}-\d{2}-\d{2})\s*$", re.IGNORECASE | re.MULTILINE
)
_ROADMAP_SECTION_RE = re.compile(
    r"^##\s+Updating This Roadmap\s*$.*?(?=^##\s|\Z)", re.MULTILINE | re.DOTALL
)

# These patterns intentionally describe repository snapshots, rather than
# ordinary operational limits such as a 15-minute retry wait or a 99% SLO.
_SNAPSHOT_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d[\d,.]*\+?\s+(?:documented\s+)?subpackages?\b", re.I),
        "documented subpackage counts must come from a maintained inventory",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d[\d,.]*\+?\s+(?:child\s+)?issues?\b", re.I),
        "issue counts are transient and must not be embedded in normative prose",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d[\d,.]*\+?\s+audit\s+dimensions?\b", re.I),
        "audit totals are transient and must not be embedded in normative prose",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d[\d,.]*\+?\s+(?:excluded\s+)?automation\s+modules?\b", re.I),
        "automation-module counts must be derived from maintained configuration",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d[\d,.]*\+?\s+tests?\s+across\b", re.I),
        "test totals change as the suite evolves and must not be snapshotted",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d+\s+of\s+\d+\s+(?:declared\s+)?(?:tools|entry points)\b", re.I),
        "partial inventory counts must be derived from maintained sources",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d+(?:\.\d+)?k\s+LoC\b", re.I),
        "repository size is a volatile snapshot and must not be normative",
    ),
    (
        "repository-snapshot-metric",
        re.compile(r"\b\d+(?:\.\d+)?%\s+of\s+the\s+(?:codebase|source)\b", re.I),
        "repository percentages are volatile snapshots and must not be normative",
    ),
)

_TEMPORARY_STATE_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "temporary-issue-state",
        re.compile(
            r"\b(?:currently|now)\s+(?:inactive|active|open|closed|blocked|pending|"
            r"in progress|unavailable|not configured|not supported)\b",
            re.I,
        ),
        "temporary operational state needs a maintained source and review trigger",
    ),
    (
        "dated-state",
        re.compile(r"\bas of\s+\d{4}-\d{2}-\d{2}\b", re.I),
        "dated snapshots need an ownership cadence or a historical-document boundary",
    ),
    (
        "dated-state",
        re.compile(r"\bhistorical status\b", re.I),
        "historical status belongs in a bounded historical record or maintained source",
    ),
)


def _relative_path(path: Path, repo_root: Path) -> str:
    """Return a stable POSIX path relative to *repo_root*."""
    return path.relative_to(repo_root).as_posix()


def _is_excluded(relative_path: str) -> bool:
    """Return whether a relative path is outside the normative corpus."""
    if any(relative_path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return True
    if any(relative_path.startswith(prefix) for prefix in _HISTORICAL_PREFIXES):
        return relative_path not in _HISTORICAL_INDEXES
    return False


def discover_normative_markdown(repo_root: Path) -> list[Path]:
    """Recursively return Markdown files in the living normative corpus.

    Accepted ADR bodies and point-in-time release-note bodies are excluded,
    while their README/index files remain in scope.  The returned paths are
    deterministic and are not modified.
    """
    if not repo_root.is_dir():
        return []
    paths = (
        path
        for path in repo_root.rglob("*.md")
        if path.is_file() and not _is_excluded(_relative_path(path, repo_root))
    )
    return sorted(paths)


def _fenced_lines(content: str) -> set[int]:
    """Return zero-based line indexes belonging to Markdown fences."""
    fenced: set[int] = set()
    active = False
    marker = ""
    for index, line in enumerate(content.splitlines()):
        match = _FENCE_RE.match(line)
        if match:
            token = match.group(1)[0]
            if not active:
                active = True
                marker = token
            elif token == marker:
                active = False
            fenced.add(index)
        elif active:
            fenced.add(index)
    return fenced


def _is_source_derived_cli_line(relative_path: str, line: str) -> bool:
    """Return whether a CLI count is delegated to the existing sync check."""
    if relative_path not in {"README.md", "COMPATIBILITY.md", "docs/index.md"}:
        return False
    lowered = line.lower()
    return "console scripts" in lowered or "cli entry points" in lowered


def scan_file(file_path: Path, repo_root: Path) -> list[Finding]:
    """Scan one normative Markdown file for volatile claims."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        return [
            Finding(
                file=_relative_path(file_path, repo_root),
                line=1,
                rule="unreadable-document",
                description=f"could not read documentation: {exc}",
            )
        ]

    relative_path = _relative_path(file_path, repo_root)
    fenced = _fenced_lines(content)
    findings: list[Finding] = []
    for index, line in enumerate(content.splitlines()):
        if index in fenced:
            continue
        # ``alert_active`` is a metric state name, not a repository snapshot;
        # its meaning is defined by the source-backed observability contract.
        if "alert_active" in line and "gauge" in line:
            continue
        if relative_path == "docs/ROADMAP.md" and line.lower().startswith("last updated:"):
            continue
        if _is_source_derived_cli_line(relative_path, line):
            continue
        for rule, pattern, description in (*_SNAPSHOT_PATTERNS, *_TEMPORARY_STATE_PATTERNS):
            if pattern.search(line):
                findings.append(
                    Finding(
                        file=relative_path,
                        line=index + 1,
                        rule=rule,
                        description=description,
                        content=line,
                    )
                )
    return findings


def scan_repository(repo_root: Path) -> list[Finding]:
    """Scan every discovered normative Markdown file for volatile claims."""
    findings: list[Finding] = []
    for path in discover_normative_markdown(repo_root):
        findings.extend(scan_file(path, repo_root))
    return findings


def _finding(
    document: str,
    rule: str,
    description: str,
    *,
    line: int = 1,
    content: str = "",
) -> Finding:
    """Construct a finding for a repository-relative documentation path."""
    return Finding(
        file=document,
        line=line,
        rule=rule,
        description=description,
        content=content,
    )


def _local_link_targets(document: Path, content: str, repo_root: Path) -> set[Path]:
    """Resolve local Markdown link targets found in *content*."""
    targets: set[Path] = set()
    for raw_target in _MARKDOWN_LINK_RE.findall(content):
        target = unquote(raw_target.split("#", 1)[0])
        if not target or target.startswith(("https://", "http://", "mailto:")):
            continue
        candidate = (document.parent / target).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError:
            continue
        targets.add(candidate)
    return targets


def _selector_exists(source: Path, selector: str) -> bool:
    """Return whether a source file contains the requested semantic selector."""
    if source.suffix == ".py":
        try:
            tree = ast.parse(source.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return False
        final_name = selector.rsplit(".", 1)[-1]
        return any(
            (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == final_name
            )
            or (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == final_name
                    for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
                )
            )
            for node in ast.walk(tree)
        )
    if source.suffix in {".yml", ".yaml"}:
        pattern = re.compile(rf"^\s*{re.escape(selector)}\s*:", re.MULTILINE)
        return bool(pattern.search(source.read_text(encoding="utf-8")))
    if source.suffix == ".md":
        heading = re.compile(rf"^#{{1,6}}\s+{re.escape(selector)}\s*$", re.MULTILINE)
        return bool(heading.search(source.read_text(encoding="utf-8")))
    return False


def validate_source_contracts(repo_root: Path) -> list[Finding]:
    """Validate ownership, triggers, source links, and semantic selectors."""
    findings: list[Finding] = []
    resolved_root = repo_root.resolve()
    for contract in SOURCE_CONTRACTS:
        document_path = repo_root / contract.document
        source_path = repo_root / contract.source
        if not document_path.is_file():
            findings.append(
                _finding(
                    contract.document,
                    "missing-maintained-document",
                    "the maintained documentation surface does not exist",
                )
            )
            continue

        content = document_path.read_text(encoding="utf-8")
        normalized = re.sub(r"\s+", " ", content).lower()
        if not re.search(r"\b(?:owner|ownership|responsibility)\b", normalized):
            findings.append(
                _finding(
                    contract.document,
                    "missing-owner",
                    "living documentation must name its owner or ownership source",
                )
            )
        if not re.search(r"\b(?:review trigger|trigger|reconcile)\b", normalized):
            findings.append(
                _finding(
                    contract.document,
                    "missing-review-trigger",
                    "living documentation must name the change or review trigger",
                )
            )

        if not source_path.is_file():
            findings.append(
                _finding(
                    contract.document,
                    "missing-maintained-source",
                    f"maintained source does not exist: {contract.source}",
                )
            )
            continue

        linked_targets = _local_link_targets(document_path, content, repo_root)
        if source_path.resolve() not in linked_targets:
            findings.append(
                _finding(
                    contract.document,
                    "invalid-source-link",
                    f"document must link to maintained source {contract.source}",
                )
            )
        try:
            source_path.resolve().relative_to(resolved_root)
        except ValueError:
            findings.append(
                _finding(
                    contract.document,
                    "source-outside-repository",
                    f"maintained source escapes repository root: {contract.source}",
                )
            )
        else:
            if not _selector_exists(source_path, contract.selector):
                findings.append(
                    _finding(
                        contract.document,
                        "missing-source-selector",
                        f"maintained source lacks semantic selector {contract.selector}",
                    )
                )
    return findings


def _quarter(year: int, quarter: int) -> tuple[int, int]:
    """Return a comparable year/quarter tuple."""
    return year, quarter


def _date_quarter(value: date) -> tuple[int, int]:
    """Return the calendar quarter containing *value*."""
    return value.year, (value.month - 1) // 3 + 1


def _validate_roadmap_sections(content: str) -> list[Finding]:
    """Validate the roadmap's explicit ownership and release trigger."""
    match = _ROADMAP_SECTION_RE.search(content)
    if match is None:
        return [
            _finding(
                "docs/ROADMAP.md",
                "missing-roadmap-maintenance-section",
                "roadmap must define its ownership and review cadence",
            )
        ]
    section = re.sub(r"\s+", " ", match.group(0)).lower()
    requirements = (
        (
            "roadmap-owner",
            "maintainer",
            "roadmap maintenance must name the responsible maintainer",
        ),
        (
            "roadmap-trigger",
            "trigger",
            "roadmap maintenance must name a review trigger",
        ),
        (
            "roadmap-release-source",
            "auto tag release",
            "roadmap cadence must link the release workflow or checklist",
        ),
        (
            "roadmap-feature-driven",
            "not date-driven",
            "roadmap cadence must be feature/fix-driven rather than calendar-driven",
        ),
    )
    return [
        _finding("docs/ROADMAP.md", rule, description)
        for rule, required, description in requirements
        if required not in section
    ]


def _validate_last_updated(content: str, *, today: date) -> list[Finding]:
    """Validate roadmap freshness against an injectable current date."""
    findings: list[Finding] = []
    focus = _CURRENT_FOCUS_RE.search(content)
    if focus is None:
        return [
            _finding(
                "docs/ROADMAP.md",
                "missing-current-focus",
                "roadmap must state its current focus quarter",
            )
        ]
    focus_quarter = _quarter(int(focus.group(2)), int(focus.group(1)))
    if focus_quarter < _date_quarter(today):
        findings.append(
            _finding(
                "docs/ROADMAP.md",
                "stale-current-focus",
                "the roadmap focus quarter is older than the injected current quarter",
                line=content[: focus.start()].count("\n") + 1,
            )
        )

    updated = _LAST_UPDATED_RE.search(content)
    if updated is None:
        findings.append(
            _finding(
                "docs/ROADMAP.md",
                "missing-last-updated",
                "roadmap must include a parseable Last updated date",
            )
        )
        return findings
    try:
        updated_date = date.fromisoformat(updated.group(1))
    except ValueError:
        findings.append(
            _finding(
                "docs/ROADMAP.md",
                "invalid-last-updated",
                "roadmap Last updated must use YYYY-MM-DD",
                line=content[: updated.start()].count("\n") + 1,
            )
        )
        return findings
    if updated_date > today:
        findings.append(
            _finding(
                "docs/ROADMAP.md",
                "future-last-updated",
                "roadmap Last updated date cannot be in the future",
                line=content[: updated.start()].count("\n") + 1,
            )
        )
    if _date_quarter(updated_date) != focus_quarter:
        findings.append(
            _finding(
                "docs/ROADMAP.md",
                "last-updated-outside-focus-quarter",
                "roadmap Last updated date must be within its stated focus quarter",
                line=content[: updated.start()].count("\n") + 1,
            )
        )
    return findings


def validate_roadmap_maintenance(
    repo_root: Path,
    *,
    today: date | None = None,
) -> list[Finding]:
    """Validate roadmap ownership, trigger, focus quarter, and freshness."""
    roadmap = repo_root / "docs" / "ROADMAP.md"
    if not roadmap.is_file():
        return [
            _finding(
                "docs/ROADMAP.md",
                "missing-roadmap",
                "docs/ROADMAP.md is required for roadmap maintenance validation",
            )
        ]
    content = roadmap.read_text(encoding="utf-8")
    effective_today = today if today is not None else date.today()
    findings = _validate_roadmap_sections(content)
    findings.extend(_validate_last_updated(content, today=effective_today))
    return findings


def _validate_source_derived_cli_counts(repo_root: Path) -> list[Finding]:
    """Delegate permitted CLI count prose to the existing source-derived check."""
    if not (repo_root / "pyproject.toml").is_file():
        return []
    try:
        expected = len(_load_scripts(repo_root))
        passed, mismatches = check_prose_counts(repo_root, expected)
    except (OSError, RuntimeError, ValueError) as exc:
        return [
            _finding(
                "pyproject.toml",
                "cli-count-check-error",
                f"could not run source-derived CLI count validation: {exc}",
            )
        ]
    if passed:
        return []
    return [
        _finding(
            mismatch.split(":", 1)[0],
            "source-derived-cli-count",
            mismatch,
        )
        for mismatch in mismatches
    ]


def validate_documentation(repo_root: Path) -> list[Finding]:
    """Run all offline documentation-maintenance checks for *repo_root*."""
    findings = scan_repository(repo_root)
    findings.extend(validate_source_contracts(repo_root))
    findings.extend(validate_roadmap_maintenance(repo_root))
    findings.extend(_validate_source_derived_cli_counts(repo_root))
    return sorted(findings, key=lambda finding: (finding.file, finding.line, finding.rule))


def format_text_report(findings: list[Finding], *, verbose: bool = False) -> str:
    """Format validation findings as human-readable text."""
    if not findings:
        return "No documentation maintenance findings.\n"
    lines = [f"Found {len(findings)} documentation maintenance finding(s):", ""]
    for finding in findings:
        lines.append(f"  [{finding.severity.value}] {finding.file}:{finding.line}")
        lines.append(f"    Rule: {finding.rule}")
        lines.append(f"    Reason: {finding.description}")
        if verbose and finding.content:
            lines.append(f"    Content: {finding.content.strip()}")
        lines.append("")
    return "\n".join(lines)


def format_json_report(findings: list[Finding]) -> str:
    """Format validation findings as a stable JSON report."""
    exit_code = 1 if findings else 0
    return json.dumps(
        {
            "findings": [finding.as_dict() for finding in findings],
            "passed": not findings,
            "exit_code": exit_code,
        },
        indent=2,
    )


def main() -> int:
    """Run the read-only documentation-maintenance CLI."""
    parser = create_validation_parser(
        "Validate ownership and currency contracts for living documentation",
        epilog="Example: %(prog)s --repo-root /path/to/repository",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Include offending source lines in text output",
    )
    args = parser.parse_args()
    repo_root = resolve_repo_root(args)
    findings = validate_documentation(repo_root)
    if args.json:
        print(format_json_report(findings))
    else:
        print(format_text_report(findings, verbose=args.verbose))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
