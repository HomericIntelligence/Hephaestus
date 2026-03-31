"""Audit documentation command examples for policy violations.

Scans all markdown files in the repository for command examples that contradict
policies defined in CLAUDE.md and reports (or counts) violations with file:line
references.

Authoritative policy source: CLAUDE.md

Policies enforced:

- ``gh pr create`` must NOT include ``--label``
- ``git commit`` must NOT use ``--no-verify``
- ``gh pr merge`` must use ``--auto --rebase`` (not ``--merge`` or ``--squash``)
- ``git push`` must NOT push directly to ``main``/``master``

Excluded paths (archived / test-fixture content that is not authoritative):

- ``docs/arxiv/``
- ``tests/claude-code/``
- ``.pixi/``
- ``build/``
- ``node_modules/``

Usage::

    hephaestus-audit-doc-policy
    hephaestus-audit-doc-policy --verbose
    hephaestus-audit-doc-policy --json

Exit codes:
    0: No violations found
    1: One or more violations found
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class Severity(str, Enum):
    """Violation severity levels."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


@dataclass
class Finding:
    """A single policy violation found in a documentation file."""

    file: str
    line: int
    content: str
    rule: str
    severity: Severity
    description: str

    def as_dict(self) -> dict[str, str | int]:
        """Return finding as a plain dictionary suitable for JSON serialisation."""
        return {
            "file": self.file,
            "line": self.line,
            "content": self.content.strip(),
            "rule": self.rule,
            "severity": self.severity.value,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Policy rules
# ---------------------------------------------------------------------------

# Each rule is (rule_id, severity, description, compiled_pattern).
# Patterns are checked against individual lines of text; a match == violation.
#
# NOTE: We specifically look for patterns *inside code blocks* only.  A naive
# per-line grep would flag prohibition text such as "Never use --no-verify".
# We therefore work at the code-block level: extract fenced bash/shell blocks,
# then apply patterns to those lines only.
_RAW_RULES: list[tuple[str, Severity, str, str]] = [
    (
        "no-label-in-pr-create",
        Severity.CRITICAL,
        "gh pr create must not use --label (labels are prohibited by CLAUDE.md)",
        # Match lines where gh pr create is followed by --label as a flag
        # (requires gh pr create to appear first, --label after — as a real flag,
        # not prose describing the flag name).  We anchor to the command by
        # requiring ``gh`` at the start of non-whitespace content on the line.
        r"^\s*(?:gh|\$\s*gh|\\)\s*(?:pr\s+create\b.*--label\b|.*--label\b.*gh\s+pr\s+create\b)",
    ),
    (
        "no-verify-in-commit",
        Severity.CRITICAL,
        "git commit must not use --no-verify (absolutely prohibited by CLAUDE.md)",
        r"git\s+commit\b.*--no-verify\b",
    ),
    (
        "wrong-merge-strategy",
        Severity.CRITICAL,
        "gh pr merge must use --auto --rebase, not --merge or --squash",
        r"gh\s+pr\s+merge\b(?!.*--auto\s+--rebase\b)(?!.*--rebase\s+--auto\b).*(?:--merge\b|--squash\b)",
    ),
    (
        "push-direct-to-main",
        Severity.CRITICAL,
        "git push must not push directly to main or master (use PRs)",
        # Exclude lines with a # comment (often used to annotate prohibited examples
        # e.g. ``git push origin main  # BLOCKED``) and --delete (branch cleanup).
        r"git\s+push\b(?!.*--delete\b)(?![^#]*#).*\b(?:origin\s+main|origin\s+master|origin\s+HEAD:main|origin\s+HEAD:master)\b",
    ),
    (
        "wrong-branch-naming",
        Severity.WARNING,
        "Branch names must follow <issue-number>-<description> format (CLAUDE.md)",
        # Flag 'git checkout -b <name>' where <name> doesn't start with digits,
        # a placeholder (<...> or {...}), or a skill path (skill/).
        # Excludes lines with a # comment (e.g. examples annotated as wrong).
        r"git\s+checkout\s+-b\s+(?!\d+-)(?![<{])(?!skill/)(?![^#]*#)\S+",
    ),
]

POLICY_RULES: list[tuple[str, Severity, str, re.Pattern[str]]] = [
    (rule_id, severity, description, re.compile(pattern))
    for rule_id, severity, description, pattern in _RAW_RULES
]

# ---------------------------------------------------------------------------
# Paths to exclude from scanning
# ---------------------------------------------------------------------------

EXCLUDED_PREFIXES: tuple[str, ...] = (
    "docs/arxiv/",
    "tests/claude-code/",
    ".pixi/",
    ".worktrees/",
    ".claude/worktrees/",
    "build/",
    "node_modules/",
)

# ---------------------------------------------------------------------------
# Core scanning logic
# ---------------------------------------------------------------------------

# Regex to extract fenced code blocks that look like shell/bash.
# Captures language tag (optional) and the block body.
_CODE_BLOCK_RE = re.compile(
    r"^```(?P<lang>[a-zA-Z0-9_\-]*)\n(?P<body>.*?)^```",
    re.MULTILINE | re.DOTALL,
)

_SHELL_LANGS: frozenset[str] = frozenset({"bash", "sh", "shell", "zsh", "console", ""})


def _extract_code_blocks(content: str) -> list[tuple[int, str]]:
    """Return list of (start_line_1indexed, block_text) for shell code blocks.

    Only fenced blocks with a shell-like language tag (or no tag) are returned.

    Args:
        content: The full markdown file content.

    Returns:
        List of ``(start_line, body)`` tuples for each matching code block.

    """
    blocks: list[tuple[int, str]] = []
    for match in _CODE_BLOCK_RE.finditer(content):
        lang = match.group("lang").lower()
        if lang not in _SHELL_LANGS:
            continue
        # Compute the line number of the opening ``` fence.
        start_line = content[: match.start()].count("\n") + 1
        blocks.append((start_line, match.group("body")))
    return blocks


def scan_file(
    file_path: Path,
    repo_root: Path,
    rules: list[tuple[str, Severity, str, re.Pattern[str]]] | None = None,
) -> list[Finding]:
    """Scan a single markdown file and return all findings.

    Args:
        file_path: Path to the markdown file to scan.
        repo_root: Repository root for computing relative paths.
        rules: Policy rules to apply (defaults to :data:`POLICY_RULES`).

    Returns:
        List of :class:`Finding` instances for any violations found.

    """
    if rules is None:
        rules = POLICY_RULES

    findings: list[Finding] = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    relative_path = str(file_path.relative_to(repo_root))

    blocks = _extract_code_blocks(content)
    for block_start_line, block_body in blocks:
        for block_line_offset, line in enumerate(block_body.splitlines(), start=1):
            absolute_line = block_start_line + block_line_offset
            for rule_id, severity, description, pattern in rules:
                if pattern.search(line):
                    findings.append(
                        Finding(
                            file=relative_path,
                            line=absolute_line,
                            content=line,
                            rule=rule_id,
                            severity=severity,
                            description=description,
                        )
                    )
    return findings


def scan_repository(
    repo_root: Path,
    rules: list[tuple[str, Severity, str, re.Pattern[str]]] | None = None,
    excluded_prefixes: tuple[str, ...] | None = None,
) -> list[Finding]:
    """Scan all non-excluded markdown files in the repository.

    Args:
        repo_root: Repository root directory to scan recursively.
        rules: Policy rules to apply (defaults to :data:`POLICY_RULES`).
        excluded_prefixes: Path prefixes to skip (defaults to :data:`EXCLUDED_PREFIXES`).

    Returns:
        List of all :class:`Finding` instances found across the repository.

    """
    if rules is None:
        rules = POLICY_RULES
    if excluded_prefixes is None:
        excluded_prefixes = EXCLUDED_PREFIXES

    all_findings: list[Finding] = []

    for md_file in sorted(repo_root.rglob("*.md")):
        relative = md_file.relative_to(repo_root)
        relative_str = str(relative).replace("\\", "/")
        if any(relative_str.startswith(prefix) for prefix in excluded_prefixes):
            continue
        all_findings.extend(scan_file(md_file, repo_root, rules=rules))

    return all_findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_text_report(findings: list[Finding], verbose: bool = False) -> str:
    """Format findings as a human-readable text report.

    Args:
        findings: List of policy violation findings.
        verbose: When True, include the violating line content.

    Returns:
        Formatted report string.

    """
    if not findings:
        return "No policy violations found.\n"

    lines: list[str] = [
        f"Found {len(findings)} policy violation(s):",
        "",
    ]
    for f in findings:
        lines.append(f"  [{f.severity.value}] {f.file}:{f.line}")
        lines.append(f"    Rule: {f.rule}")
        lines.append(f"    Reason: {f.description}")
        if verbose:
            lines.append(f"    Content: {f.content.strip()}")
        lines.append("")

    return "\n".join(lines)


def format_json_report(findings: list[Finding]) -> str:
    """Format findings as a JSON string.

    Args:
        findings: List of policy violation findings.

    Returns:
        JSON-serialised findings.

    """
    return json.dumps([f.as_dict() for f in findings], indent=2)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the documentation policy audit.

    Returns:
        Exit code (0 if no violations, 1 if violations found).

    """
    parser = argparse.ArgumentParser(
        description="Audit documentation command examples for policy violations",
        epilog="Example: %(prog)s --directory docs/ --verbose",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Directory to scan (default: repository root)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root (default: auto-detect)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print violating line content in text output",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        metavar="PREFIX",
        default=None,
        help="Additional path prefix to exclude (may be repeated)",
    )

    args = parser.parse_args()
    repo_root: Path = args.repo_root or get_repo_root()
    scan_root: Path = args.directory or repo_root

    excluded: tuple[str, ...] = EXCLUDED_PREFIXES
    if args.exclude:
        excluded = excluded + tuple(args.exclude)

    findings = scan_repository(scan_root, excluded_prefixes=excluded)

    if args.json_output:
        print(format_json_report(findings))
    else:
        print(format_text_report(findings, verbose=args.verbose))

    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
