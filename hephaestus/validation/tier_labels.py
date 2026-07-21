"""Enforce tier label consistency across all project Markdown files.

Scans Markdown files for incorrect tier label mappings, e.g., calling T2
"Skills" when its canonical name is "Tooling", or T3 "Tooling" when it
should be "Delegation".

Canonical tier mapping (authoritative source: AGENTS.md):
  T0 = Prompts
  T1 = Skills
  T2 = Tooling
  T3 = Delegation
  T4 = Hierarchy
  T5 = Hybrid
  T6 = Super

Mismatch patterns detected: a tier ID followed (on the same line) by any
tier name that does not match the canonical name for that tier number.

Usage::

    hephaestus-check-tier-labels
    hephaestus-check-tier-labels --verbose
    hephaestus-check-tier-labels --json
    hephaestus-check-tier-labels --glob "**/*.md"
    hephaestus-check-tier-labels --repo-root /path/to/repo
    hephaestus-check-tier-labels --directory /path/to/dir
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

# ---------------------------------------------------------------------------
# Canonical mapping (source of truth)
# ---------------------------------------------------------------------------

CANONICAL_TIERS: dict[str, str] = {
    "T0": "Prompts",
    "T1": "Skills",
    "T2": "Tooling",
    "T3": "Delegation",
    "T4": "Hierarchy",
    "T5": "Hybrid",
    "T6": "Super",
}

# Default directories to skip when scanning the repository.
_DEFAULT_EXCLUDES: frozenset[str] = frozenset(
    {".venv", "build", ".git", ".worktrees", "node_modules"}
)

# Matches a tier ID followed (on the same line) by any known tier name,
# e.g. "T3/Tooling", "T4 (Delegation)", "T5–Hierarchy".
_MISMATCH_RE = re.compile(
    r"\b(T[0-6])\s*[/(–\-]\s*(Prompts|Skills|Tooling|Delegation|Hierarchy|Hybrid|Super)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# BAD_PATTERNS — fully derived from CANONICAL_TIERS
# ---------------------------------------------------------------------------
#
# One (regex, reason) entry per (tier, wrong_name) pair where wrong_name is
# the canonical name of some OTHER tier.  Adding a tier to CANONICAL_TIERS
# expands BAD_PATTERNS automatically — no parallel structure to maintain.
#
# Regex: tier ID, then up to 15 chars, then the wrong canonical name.  The
# 15-char window matches the bounded form used by the previous hand-written
# table (".{0,10}" plus slack for separators like "/", " is ", "(").
BAD_PATTERNS: list[tuple[str, str]] = [
    (
        rf"\b{tier}\b.{{0,15}}{re.escape(wrong_name)}",
        f"{tier} is {correct_name}, not {wrong_name}",
    )
    for tier, correct_name in CANONICAL_TIERS.items()
    for wrong_name in CANONICAL_TIERS.values()
    if wrong_name != correct_name
]

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class TierLabelFinding:
    """A tier label mismatch found in a Markdown file."""

    file: str
    line: int
    tier: str
    found_name: str
    expected_name: str
    raw_text: str

    def format(self) -> str:
        """Return a human-readable description of this finding."""
        return (
            f"  {self.file}:{self.line}\n"
            f"    Found: {self.tier}/{self.found_name}  "
            f"Expected: {self.tier}/{self.expected_name}\n"
            f"    Text: {self.raw_text!r}\n"
        )


# ---------------------------------------------------------------------------
# Detection logic
# ---------------------------------------------------------------------------


def _collect_mismatches(
    path: Path,
    canonical_tiers: dict[str, str] | None = None,
) -> list[TierLabelFinding]:
    """Scan *path* for tier label mismatches and return all findings.

    Args:
        path: Path to the Markdown (or YAML) file to inspect.
        canonical_tiers: Optional custom tier mapping.  Defaults to
            ``CANONICAL_TIERS`` when ``None``.

    Returns:
        List of TierLabelFinding instances for each mismatch found.

    """
    tier_map = canonical_tiers if canonical_tiers is not None else CANONICAL_TIERS
    findings: list[TierLabelFinding] = []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings

    for lineno, line in enumerate(content.splitlines(), start=1):
        for match in _MISMATCH_RE.finditer(line):
            tier = match.group(1).upper()
            found_name = match.group(2)
            canonical = tier_map.get(tier, "")
            if canonical and found_name.lower() != canonical.lower():
                findings.append(
                    TierLabelFinding(
                        file=str(path),
                        line=lineno,
                        tier=tier,
                        found_name=found_name,
                        expected_name=canonical,
                        raw_text=line.rstrip(),
                    )
                )

    return findings


def find_violations(content: str) -> list[tuple[int, str, str, str]]:
    """Find lines matching known-bad tier label patterns.

    This is the legacy API kept for backwards-compatibility with existing
    tests and callers.  New code should use ``_collect_mismatches``.

    Args:
        content: Text content to scan.

    Returns:
        List of (line_number, line_text, pattern, reason) tuples.

    """
    violations: list[tuple[int, str, str, str]] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for pattern, reason in BAD_PATTERNS:
            if re.search(pattern, line):
                violations.append((lineno, line, pattern, reason))
    return violations


# ---------------------------------------------------------------------------
# Repository scan
# ---------------------------------------------------------------------------


def scan_repository(
    repo_root: Path,
    glob: str = "**/*.md",
    excludes: set[str] | None = None,
    canonical_tiers: dict[str, str] | None = None,
) -> list[TierLabelFinding]:
    """Scan all Markdown files in *repo_root* for tier label mismatches.

    Args:
        repo_root: Root directory to scan from.
        glob: Glob pattern relative to *repo_root* (default ``**/*.md``).
        excludes: Set of directory-name segments to skip.  Defaults to
            ``_DEFAULT_EXCLUDES`` when ``None``.
        canonical_tiers: Optional custom tier mapping.  Defaults to
            ``CANONICAL_TIERS`` when ``None``.

    Returns:
        Aggregated list of TierLabelFinding across all matched files.

    """
    if excludes is None:
        excludes = set(_DEFAULT_EXCLUDES)

    all_findings: list[TierLabelFinding] = []

    for md_file in sorted(repo_root.glob(glob)):
        # Skip any file whose path contains an excluded directory segment.
        if any(part in excludes for part in md_file.parts):
            continue
        findings = _collect_mismatches(md_file, canonical_tiers=canonical_tiers)
        for f in findings:
            # Store path relative to repo root for cleaner output.
            with contextlib.suppress(ValueError):
                f.file = str(md_file.relative_to(repo_root))
        all_findings.extend(findings)

    return all_findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def format_report(findings: list[TierLabelFinding]) -> str:
    """Format findings as a human-readable text report.

    Args:
        findings: List of TierLabelFinding instances.

    Returns:
        Multi-line string suitable for stdout.

    """
    if not findings:
        return "No tier label mismatches found.\n"

    lines: list[str] = [f"Found {len(findings)} tier label mismatch(es):", ""]
    for f in findings:
        lines.append(f.format())
    return "\n".join(lines)


def format_json(findings: list[TierLabelFinding]) -> str:
    """Format findings as a JSON string.

    Args:
        findings: List of TierLabelFinding instances.

    Returns:
        JSON-encoded string.

    """
    return json.dumps([asdict(f) for f in findings], indent=2)


# ---------------------------------------------------------------------------
# Legacy single-file checker (preserved for backwards-compatibility)
# ---------------------------------------------------------------------------


def check_tier_label_consistency(target: Path) -> int:
    """Check *target* for known-bad tier label patterns.

    Args:
        target: Path to the Markdown file to inspect.

    Returns:
        0 if no violations found, 1 otherwise.

    """
    if not target.is_file():
        print(f"ERROR: File not found: {target}", file=sys.stderr)
        return 1

    content = target.read_text(encoding="utf-8")
    violations = find_violations(content)

    if violations:
        print(
            f"ERROR: Found {len(violations)} tier label mismatch(es) in {target}:",
            file=sys.stderr,
        )
        for lineno, line, pattern, reason in violations:
            print(f"  Line {lineno}: {line.rstrip()}", file=sys.stderr)
            print(f"    Pattern: {pattern!r} — {reason}", file=sys.stderr)
        return 1

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the tier label consistency check.

    Returns:
        Exit code: 0 clean, 1 mismatches found, 2 I/O error.

    """
    parser = create_validation_parser(
        (
            "Enforce tier label consistency in Markdown files.  "
            "By default scans all *.md files in the repository."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s\n"
            "  %(prog)s --verbose\n"
            "  %(prog)s --json\n"
            "  %(prog)s --directory /path/to/dir\n"
            "  %(prog)s --glob '**/*.md' --exclude build --exclude .venv"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Directory to scan (default: repository root).",
    )
    parser.add_argument(
        "--glob",
        default="**/*.md",
        help="Glob pattern to match Markdown files (default: **/*.md).",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        dest="excludes",
        metavar="DIR",
        default=[],
        help=("Directory name to exclude (repeatable, default: .venv build .git .worktrees)."),
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print details for each mismatch found.",
    )

    args = parser.parse_args()

    # Resolve repository root.
    try:
        repo_root = resolve_repo_root(args)
    except Exception as exc:
        print(f"ERROR: Could not determine repository root: {exc}", file=sys.stderr)
        return 2

    scan_root = args.directory if args.directory is not None else repo_root

    excludes: set[str] = set(_DEFAULT_EXCLUDES)
    if args.excludes:
        excludes = excludes | set(args.excludes)

    try:
        findings = scan_repository(scan_root, glob=args.glob, excludes=excludes)
    except OSError as exc:
        print(f"ERROR: I/O error during scan: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(format_json(findings))
    elif args.verbose or findings:
        print(format_report(findings), end="")
        if findings:
            return 1
    else:
        print("No tier label mismatches found.")

    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
