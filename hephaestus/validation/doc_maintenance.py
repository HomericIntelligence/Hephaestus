"""Validate ownership and currency contracts for living documentation.

The validator is intentionally read-only.  It scans normative Markdown,
checks the small set of source-backed documentation contracts, and reports
claims that are likely to become stale without an explicit maintenance owner.
Generated material, examples, accepted ADR bodies, and release-note bodies
are outside the living-documentation boundary.

Usage::

    python -m hephaestus.validation.doc_maintenance --repo-root .
    python -m hephaestus.validation.doc_maintenance --repo-root . --json
"""

from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from hephaestus.cli.utils import create_validation_parser, format_output, resolve_repo_root
from hephaestus.scripts_lib.check_cli_table_sync import _load_scripts, check_prose_counts

EXCLUDED_PREFIXES: tuple[str, ...] = (
    ".git/",
    ".pytest_cache/",
    ".venv/",
    ".worktrees/",
    "build/",
    "tests/fixtures/",
)


@dataclass(frozen=True)
class Finding:
    """A documentation maintenance violation."""

    file: str
    line: int
    rule: str
    message: str
    severity: str = "error"
    content: str = ""

    def as_dict(self) -> dict[str, str | int]:
        """Return a JSON-serializable representation of the finding."""
        return {
            "file": self.file,
            "line": self.line,
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity,
            "content": self.content.strip(),
        }


@dataclass(frozen=True)
class SourceContract:
    """Describe a source path and semantic selector cited by a document."""

    document: str
    source: str
    selector: str


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
        document="docs/ci/required-checks.md",
        source=".github/workflows/test.yml",
        selector="jobs",
    ),
    SourceContract(
        document="docs/specs/2026-07-16-jinja-prompt-templates-design.md",
        source="hephaestus/prompts/templates/default",
        selector="",
    ),
    SourceContract(
        document="docs/ROADMAP.md",
        source="docs/RELEASING.md",
        selector="Pre-Release Checklist",
    ),
)

_EXCLUDED_DOCUMENT_DIRS = ("docs/adr/", "docs/release-notes/")
_LIVING_RECORD_NAMES = frozenset({"README.md", "index.md"})
_ADR_STATUS_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*Status\*\*\s*:|\*\*Status:\*\*|Status\s*:)"
    r"\s*(?P<status>.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_ACCEPTED_ADR_STATUS_RE = re.compile(
    r"Accepted(?:\s*\([^\r\n)]+\))?",
    re.IGNORECASE,
)
_MARKDOWN_SECTION_RE = re.compile(r"^##\s+", re.MULTILINE)
_ROADMAP_UPDATE_SECTION_RE = re.compile(r"^##\s+Updating This Roadmap\s*$", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*(`{3,}|~{3,})")
_DATE_STATE_RE = re.compile(r"\b(?:as of|last updated:)\s+\d{4}-\d{2}-\d{2}\b", re.IGNORECASE)
_TEMPORARY_STATE_RE = re.compile(
    r"\b(?:(?:currently|temporarily)\s+(?:inactive|unavailable|planned)|"
    r"(?:issue|pr)\s+#\d+\s+(?:is|was|remains?)\s+"
    r"(?:open|closed|active|inactive|in progress|blocked))\b",
    re.IGNORECASE,
)
_SNAPSHOT_METRIC_RES = (
    re.compile(r"\b\d+(?:\.\d+)?[kK]?\s+LoC\b"),
    re.compile(r"\b\d+\+?\s+(?:documented\s+)?(?:Python\s+)?subpackages?\b"),
    re.compile(r"\b\d+\+?\s+(?:documented\s+)?packages?\b"),
    re.compile(r"\b\d+\+?\s+tests?\s+(?:across|in|total)\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+of\s+\d+\s+(?:declared\s+)?(?:tools|entry points|modules)\b"),
    re.compile(r"\b\d+(?:\.\d+)?%\s+of\s+the\s+(?:codebase|source tree)\b", re.IGNORECASE),
)
_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
_CURRENT_FOCUS_RE = re.compile(r"^##\s+Current Focus \(Q([1-4])\s+(\d{4})\)\s*$", re.MULTILINE)
_LAST_UPDATED_RE = re.compile(r"^Last updated:\s*(\S+)\s*$", re.MULTILINE)

_MAINTENANCE_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "docs/documentation-maintenance.md": (
        "Ownership follows",
        "Review trigger",
        "Maintained source",
    ),
    "docs/architecture.md": (
        "Maintenance:",
        "CODEOWNERS",
        "Changes to a cited",
    ),
    "docs/specs/2026-07-16-jinja-prompt-templates-design.md": (
        "**Owner:**",
        "**Review trigger:**",
        "**Maintained sources:**",
    ),
    "docs/ci/required-checks.md": (
        "## Maintenance",
        "**Owner:**",
        "**Trigger:**",
    ),
}


def _relative_path(path: Path, repo_root: Path) -> str:
    """Return a stable POSIX path relative to *repo_root*."""
    return path.relative_to(repo_root).as_posix()


def _is_accepted_adr(file_path: Path) -> bool:
    """Return whether *file_path* declares an accepted ADR status."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    first_section = _MARKDOWN_SECTION_RE.search(content)
    metadata = content[: first_section.start()] if first_section else content
    statuses = [
        match.group("status").strip().strip("*_` ") for match in _ADR_STATUS_RE.finditer(metadata)
    ]
    return bool(statuses) and all(
        _ACCEPTED_ADR_STATUS_RE.fullmatch(status) is not None for status in statuses
    )


def _is_excluded(relative_path: str, file_path: Path) -> bool:
    """Return whether a repository-relative path is outside the scan boundary."""
    if any(relative_path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
        return True
    if relative_path.startswith(_EXCLUDED_DOCUMENT_DIRS[0]):
        return Path(relative_path).name not in _LIVING_RECORD_NAMES and _is_accepted_adr(file_path)
    if relative_path.startswith(_EXCLUDED_DOCUMENT_DIRS[1]):
        return Path(relative_path).name not in _LIVING_RECORD_NAMES
    return False


def discover_normative_markdown(repo_root: Path) -> list[Path]:
    """Recursively discover living Markdown files under *repo_root*.

    Args:
        repo_root: Repository root to scan.

    Returns:
        Sorted paths for Markdown files in the normative documentation scope.

    """
    return sorted(
        path
        for path in repo_root.rglob("*.md")
        if path.is_file() and not _is_excluded(_relative_path(path, repo_root), path)
    )


def _prose_lines(content: str) -> list[tuple[int, str]]:
    """Return non-fenced lines with their original one-based line numbers."""
    lines: list[tuple[int, str]] = []
    in_fence = False
    fence_char = ""
    for line_number, line in enumerate(content.splitlines(), start=1):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(1)
            if not in_fence:
                in_fence = True
                fence_char = marker[0]
            elif marker[0] == fence_char:
                in_fence = False
            continue
        if not in_fence:
            lines.append((line_number, line))
    return lines


def _finding(
    relative_path: str,
    line_number: int,
    rule: str,
    message: str,
    content: str,
) -> Finding:
    """Build a standard line-oriented finding."""
    return Finding(
        file=relative_path,
        line=line_number,
        rule=rule,
        message=message,
        content=content,
    )


def validate_volatile_claims(file_path: Path, repo_root: Path) -> list[Finding]:
    """Find unowned dated-state and repository-snapshot claims in one document."""
    relative_path = _relative_path(file_path, repo_root)
    content = file_path.read_text(encoding="utf-8", errors="replace")
    findings: list[Finding] = []
    for line_number, line in _prose_lines(content):
        dated_state_match = _DATE_STATE_RE.search(line)
        if dated_state_match and not (
            relative_path == "docs/ROADMAP.md"
            and dated_state_match.group(0).lower().startswith("last updated:")
        ):
            findings.append(
                _finding(
                    relative_path,
                    line_number,
                    "dated-state",
                    "dated operational state must be maintained by a source and trigger",
                    line,
                )
            )
        if _TEMPORARY_STATE_RE.search(line):
            findings.append(
                _finding(
                    relative_path,
                    line_number,
                    "temporary-state",
                    "temporary issue or service state must be derived from a maintained source",
                    line,
                )
            )
        if any(pattern.search(line) for pattern in _SNAPSHOT_METRIC_RES):
            findings.append(
                _finding(
                    relative_path,
                    line_number,
                    "snapshot-metric",
                    "repository-size or test-count snapshots are not maintained documentation",
                    line,
                )
            )
    return findings


def _source_selector_exists(source_path: Path, selector: str) -> bool:
    """Check a Python symbol, YAML key, or Markdown heading selector."""
    if source_path.is_dir():
        return not selector and any(path.is_file() for path in source_path.rglob("*"))
    if not selector:
        return source_path.exists()
    suffix = source_path.suffix.lower()
    text = source_path.read_text(encoding="utf-8", errors="replace")
    if suffix == ".py":
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return False
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == selector
            ):
                return True
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Name) and target.id == selector for target in targets
                ):
                    return True
        return False
    if suffix in {".yml", ".yaml"}:
        return re.search(rf"^\s*{re.escape(selector)}\s*:", text, re.MULTILINE) is not None
    if suffix == ".md":
        heading_re = re.compile(rf"^#{{1,6}}\s+{re.escape(selector)}\s*$")
        return any(heading_re.match(line) for _, line in _prose_lines(text))
    return False


def _document_links_source(document_path: Path, source_path: Path) -> bool:
    """Return whether a Markdown document links to the exact local source path."""
    content = document_path.read_text(encoding="utf-8", errors="replace")
    for raw_target in _MARKDOWN_LINK_RE.findall(content):
        target = raw_target.strip().strip("<>")
        parsed = urlparse(target)
        if parsed.scheme or parsed.netloc:
            continue
        target_path = unquote(parsed.path)
        if not target_path:
            continue
        if (document_path.parent / target_path).resolve() == source_path.resolve():
            return True
    return False


def _source_finding(contract: SourceContract, rule: str, message: str) -> Finding:
    """Create a finding anchored to a source contract's document."""
    return Finding(file=contract.document, line=1, rule=rule, message=message)


def validate_source_contracts(
    repo_root: Path,
    *,
    contracts: tuple[SourceContract, ...] = SOURCE_CONTRACTS,
) -> list[Finding]:
    """Validate cited source paths, links, and semantic selectors."""
    findings: list[Finding] = []
    resolved_root = repo_root.resolve()
    for contract in contracts:
        document_path = repo_root / contract.document
        source_path = repo_root / contract.source
        if not document_path.is_file():
            findings.append(
                _source_finding(contract, "source-document", "cited document is missing")
            )
            continue
        try:
            source_path.resolve().relative_to(resolved_root)
        except ValueError:
            findings.append(
                _source_finding(
                    contract,
                    "source-path",
                    f"source must remain inside repository: {contract.source}",
                )
            )
            continue
        if not source_path.exists():
            findings.append(
                _source_finding(
                    contract, "source-path", f"source does not exist: {contract.source}"
                )
            )
            continue
        if not _document_links_source(document_path, source_path):
            findings.append(
                _source_finding(
                    contract,
                    "source-link",
                    f"document must link to maintained source: {contract.source}",
                )
            )
        if not _source_selector_exists(source_path, contract.selector):
            findings.append(
                _source_finding(
                    contract,
                    "source-selector",
                    f"maintained source lacks selector: {contract.selector}",
                )
            )
    return findings


def _quarter_for(value: date) -> tuple[int, int]:
    """Return the calendar quarter containing *value*."""
    return value.year, (value.month - 1) // 3 + 1


def _roadmap_update_section(content: str) -> str:
    """Return the normalized ``Updating This Roadmap`` section body."""
    section_match = _ROADMAP_UPDATE_SECTION_RE.search(content)
    if section_match is None:
        return ""
    next_section = _MARKDOWN_SECTION_RE.search(content, section_match.end())
    section_end = next_section.start() if next_section else len(content)
    return re.sub(r"\s+", " ", content[section_match.end() : section_end]).casefold()


def _validate_roadmap_sections(content: str) -> list[Finding]:
    """Validate roadmap ownership, trigger, focus, and source metadata."""
    findings: list[Finding] = []
    if _CURRENT_FOCUS_RE.search(content) is None:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "focus-quarter",
                "roadmap must state a current focus quarter",
            )
        )
    update_section = _roadmap_update_section(content)
    required_phrases = {
        "roadmap-cadence": ("release-driven", "Auto Tag Release", "not date-driven"),
        "roadmap-ownership": ("Trigger", "Responsibility", "maintainer"),
        "roadmap-source": ("RELEASING.md",),
    }
    for rule, phrases in required_phrases.items():
        if not all(phrase.casefold() in update_section for phrase in phrases):
            message = (
                "roadmap update cadence must be release-driven through Auto Tag Release, "
                "not date-driven"
                if rule == "roadmap-cadence"
                else "roadmap must document its owner, review trigger, and maintained source"
            )
            findings.append(
                Finding(
                    "docs/ROADMAP.md",
                    1,
                    rule,
                    message,
                )
            )
    return findings


def _validate_last_updated(content: str, *, today: date) -> list[Finding]:
    """Validate roadmap date syntax and freshness against an injected date."""
    findings: list[Finding] = []
    focus_match = _CURRENT_FOCUS_RE.search(content)
    if focus_match is None:
        return findings
    focus_quarter = (int(focus_match.group(2)), int(focus_match.group(1)))
    current_quarter = _quarter_for(today)
    if focus_quarter < current_quarter:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "stale-current-focus",
                "roadmap current focus quarter is older than the supplied current quarter",
            )
        )

    updated_match = _LAST_UPDATED_RE.search(content)
    if updated_match is None:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "last-updated",
                "roadmap must contain Last updated: YYYY-MM-DD",
            )
        )
        return findings
    try:
        last_updated = datetime.strptime(updated_match.group(1), "%Y-%m-%d").date()
    except ValueError:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "last-updated",
                "roadmap Last updated value must be an ISO date",
            )
        )
        return findings
    if last_updated > today:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "future-last-updated",
                "roadmap Last updated date cannot be in the future",
            )
        )
    if _quarter_for(last_updated) < focus_quarter:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "last-updated-before-focus",
                "roadmap Last updated date must be within the stated focus quarter",
            )
        )
    elif _quarter_for(last_updated) > focus_quarter:
        findings.append(
            Finding(
                "docs/ROADMAP.md",
                1,
                "last-updated-outside-focus-quarter",
                "roadmap Last updated date must be within the stated focus quarter",
            )
        )
    return findings


def validate_roadmap_maintenance(repo_root: Path, *, today: date | None = None) -> list[Finding]:
    """Validate roadmap ownership and deterministic freshness rules."""
    effective_today = today or date.today()
    roadmap_path = repo_root / "docs" / "ROADMAP.md"
    if not roadmap_path.is_file():
        return [Finding("docs/ROADMAP.md", 1, "roadmap-missing", "roadmap is required")]
    content = roadmap_path.read_text(encoding="utf-8", errors="replace")
    findings = _validate_roadmap_sections(content)
    findings.extend(_validate_last_updated(content, today=effective_today))
    return findings


def _validate_maintenance_metadata(repo_root: Path) -> list[Finding]:
    """Ensure each volatile source surface states owner and review trigger."""
    findings: list[Finding] = []
    for relative_path, required_phrases in _MAINTENANCE_REQUIREMENTS.items():
        path = repo_root / relative_path
        if not path.is_file():
            findings.append(
                Finding(
                    relative_path,
                    1,
                    "maintenance-document",
                    "maintenance contract is missing",
                )
            )
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for phrase in required_phrases:
            if phrase.lower() not in content.lower():
                findings.append(
                    Finding(
                        relative_path,
                        1,
                        "maintenance-metadata",
                        f"maintenance contract is missing: {phrase}",
                    )
                )
    return findings


def _validate_cli_counts(repo_root: Path) -> list[Finding]:
    """Delegate documented CLI count validation to its authoritative checker."""
    pyproject_path = repo_root / "pyproject.toml"
    if not pyproject_path.is_file():
        return [
            Finding(
                "pyproject.toml",
                1,
                "cli-source",
                "pyproject.toml is required for CLI counts",
            )
        ]
    try:
        expected_count = len(_load_scripts(repo_root))
    except (OSError, RuntimeError, ValueError) as exc:
        return [Finding("pyproject.toml", 1, "cli-source", f"unable to load CLI source: {exc}")]
    passed, mismatches = check_prose_counts(repo_root, expected_count)
    if passed:
        return []
    return [Finding("pyproject.toml", 1, "cli-count", mismatch) for mismatch in mismatches]


def validate_documentation(repo_root: Path) -> list[Finding]:
    """Run the complete read-only documentation maintenance policy."""
    findings: list[Finding] = []
    for path in discover_normative_markdown(repo_root):
        findings.extend(validate_volatile_claims(path, repo_root))
    findings.extend(validate_source_contracts(repo_root))
    findings.extend(validate_roadmap_maintenance(repo_root))
    findings.extend(_validate_maintenance_metadata(repo_root))
    findings.extend(_validate_cli_counts(repo_root))
    return findings


def _print_findings(findings: list[Finding]) -> None:
    """Print human-readable findings."""
    if not findings:
        print("OK: normative documentation maintenance checks passed")
        return
    for finding in findings:
        location = f"{finding.file}:{finding.line}"
        print(f"{location}: {finding.rule}: {finding.message}")
    print(f"Found {len(findings)} documentation maintenance finding(s).")


def main() -> int:
    """Run the documentation maintenance CLI and return its exit status."""
    parser = create_validation_parser(
        "Validate ownership and currency contracts for normative documentation",
        epilog="Example: %(prog)s --repo-root /path/to/repository --json",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Print passing checks")
    args = parser.parse_args()
    repo_root = resolve_repo_root(args)
    findings = validate_documentation(repo_root)
    if args.json:
        payload = {
            "findings": [finding.as_dict() for finding in findings],
            "passed": not findings,
            "exit_code": 0 if not findings else 1,
        }
        print(format_output(payload, "json"))
    else:
        _print_findings(findings)
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
