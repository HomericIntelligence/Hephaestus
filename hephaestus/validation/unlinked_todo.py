"""Enforce the linked-tech-debt-marker convention (docs/TECH_DEBT.md).

Every ``# TODO`` / ``# FIXME`` / ``# HACK`` comment in scanned source MUST
reference a tracking issue using the ``# TODO(#N): explanation`` form. Bare,
unlinked markers are rejected.

If this hook misfires in a way you cannot fix locally, bypass it with
``SKIP=check-no-unlinked-todo git commit -S -s ...`` (do NOT use
``--no-verify``, which skips ALL hooks including signing).

Usage::
    hephaestus-check-unlinked-todo
    hephaestus-check-unlinked-todo --json
    hephaestus-check-unlinked-todo --repo-root /path/to/repo
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

# Dirs (relative to repo root) whose .py files the policy governs.
SCANNED_ROOTS: tuple[str, ...] = ("hephaestus", "scripts")

# The validator module itself legitimately contains the literal marker
# words in its docstring/strings; excluding it keeps the gate green on the
# shipped tree (self-reference exemption).
_EXCLUDED_RELPATHS: frozenset[str] = frozenset(
    {
        "hephaestus/validation/unlinked_todo.py",
    }
)

# A marker in a comment: ``#``, optional space, keyword, word boundary. The
# ``#`` lead-in anchors on comment context so a string literal like ``"TODO"``
# is not falsely flagged (skill: anchor on the prefix, not a free substring).
_MARKER_RE = re.compile(r"#\s*(TODO|FIXME|HACK)\b(.*)$")
# A linked marker: keyword immediately followed by ``(#<digits>)``.
_LINK_RE = re.compile(r"^\s*\(#\d+\)")


@dataclass
class UnlinkedMarkerFinding:
    """A single bare marker lacking a ``(#N)`` tracking-issue reference."""

    path: str  # repo-relative
    line: int
    marker: str  # "TODO" | "FIXME" | "HACK"
    detail: str


def scan_file(path: Path, relpath: str) -> list[UnlinkedMarkerFinding]:
    """Return findings for every bare marker in *path*.

    Args:
        path: Filesystem path to the source file to scan.
        relpath: Repo-relative path used in findings and reports.

    Returns:
        A list of :class:`UnlinkedMarkerFinding`, one per bare marker; empty
        when every marker in the file uses the linked ``# TODO(#N)`` form.

    """
    findings: list[UnlinkedMarkerFinding] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        m = _MARKER_RE.search(line)
        if not m:
            continue
        if _LINK_RE.match(m.group(2)):
            continue  # linked form `# TODO(#N): ...` — allowed
        findings.append(
            UnlinkedMarkerFinding(
                path=relpath,
                line=lineno,
                marker=m.group(1),
                detail=(
                    f"{relpath}:{lineno} has a bare `# {m.group(1)}` marker; "
                    f"use the `# {m.group(1)}(#N): explanation` form "
                    f"(see docs/TECH_DEBT.md)"
                ),
            )
        )
    return findings


def find_violations(repo_root: Path) -> list[UnlinkedMarkerFinding]:
    """Scan all governed ``.py`` files under *repo_root* for bare markers.

    Args:
        repo_root: Repository root whose ``SCANNED_ROOTS`` subtrees are walked.

    Returns:
        A list of :class:`UnlinkedMarkerFinding` across all scanned files;
        empty when every marker references a tracking issue.

    """
    findings: list[UnlinkedMarkerFinding] = []
    for root in SCANNED_ROOTS:
        base = repo_root / root
        if not base.is_dir():
            continue
        for py in sorted(base.rglob("*.py")):
            relpath = py.relative_to(repo_root).as_posix()
            if relpath in _EXCLUDED_RELPATHS:
                continue
            findings.extend(scan_file(py, relpath))
    return findings


def format_report(findings: list[UnlinkedMarkerFinding]) -> str:
    """Render *findings* as a human-readable text report."""
    if not findings:
        return "OK: every tech-debt marker references a tracking issue."
    lines = [f"FAIL: {len(findings)} unlinked marker(s):"]
    lines.extend(f"  [{f.marker}] {f.detail}" for f in findings)
    return "\n".join(lines)


def format_json(findings: list[UnlinkedMarkerFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(f) for f in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-unlinked-todo``."""
    parser = create_validation_parser(__doc__)
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    findings = find_violations(repo_root)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
