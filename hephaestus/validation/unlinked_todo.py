"""Enforce issue-linked TODO/FIXME/HACK comments.

ProjectHephaestus tracks tech debt in GitHub issues. Python comments that use
``TODO``, ``FIXME``, or ``HACK`` markers must use the documented
``# TODO(#N): explanation`` form so the debt remains traceable.

Usage::

    hephaestus-check-unlinked-todo
    hephaestus-check-unlinked-todo --json
    hephaestus-check-unlinked-todo --repo-root /path/to/repo
"""

from __future__ import annotations

import json
import re
import sys
import tokenize
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

from hephaestus.cli.utils import create_validation_parser, resolve_repo_root

DEFAULT_SCAN_PATHS: Final[tuple[Path, ...]] = (Path("hephaestus"), Path("scripts"))
_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"^#\s*(TODO|FIXME|HACK)\b")
_LINKED_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"^#\s*(TODO|FIXME|HACK)\(#\d+\):\s+\S")


@dataclass(frozen=True)
class UnlinkedTodoFinding:
    """A TODO/FIXME/HACK marker that lacks a tracking issue reference."""

    path: str
    line: int
    marker: str
    text: str


def _display_path(path: Path, repo_root: Path) -> str:
    """Return a stable repo-relative path when *path* is under *repo_root*."""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _iter_python_files(paths: list[Path]) -> list[Path]:
    """Return all Python files under *paths* in deterministic order."""
    files: list[Path] = []
    for path in paths:
        if path.is_file():
            if path.suffix == ".py":
                files.append(path)
            continue
        if path.is_dir():
            files.extend(p for p in path.rglob("*.py") if p.is_file())
    return sorted(files)


def scan_file(path: Path, repo_root: Path) -> list[UnlinkedTodoFinding]:
    """Return unlinked TODO/FIXME/HACK markers found in one Python file.

    The scan uses Python's tokenizer so marker-like text in strings or
    docstrings does not become a false violation.

    Args:
        path: Python source file to scan.
        repo_root: Repository root used for stable report paths.

    Returns:
        A list of unlinked marker findings, empty when the file is compliant.

    """
    findings: list[UnlinkedTodoFinding] = []
    with tokenize.open(path) as handle:
        for token in tokenize.generate_tokens(handle.readline):
            if token.type != tokenize.COMMENT:
                continue
            comment = token.string.strip()
            marker_match = _MARKER_RE.match(comment)
            if marker_match is None or _LINKED_MARKER_RE.match(comment):
                continue
            findings.append(
                UnlinkedTodoFinding(
                    path=_display_path(path, repo_root),
                    line=token.start[0],
                    marker=marker_match.group(1),
                    text=comment,
                )
            )
    return findings


def find_unlinked_todos(
    repo_root: Path,
    paths: list[Path] | None = None,
) -> list[UnlinkedTodoFinding]:
    """Return all unlinked debt markers under *paths*.

    Args:
        repo_root: Repository root used to resolve relative scan paths.
        paths: Optional files or directories to scan. Relative paths are
            resolved under *repo_root*. Defaults to ``hephaestus/`` and
            ``scripts/``.

    Returns:
        A sorted list of unlinked marker findings.

    """
    raw_paths = paths if paths is not None else list(DEFAULT_SCAN_PATHS)
    resolved_paths = [path if path.is_absolute() else repo_root / path for path in raw_paths]
    findings: list[UnlinkedTodoFinding] = []
    for file_path in _iter_python_files(resolved_paths):
        findings.extend(scan_file(file_path, repo_root))
    return findings


def format_report(findings: list[UnlinkedTodoFinding]) -> str:
    """Render *findings* as a human-readable report."""
    if not findings:
        return "OK: every TODO/FIXME/HACK comment references a tracking issue."
    lines = [f"FAIL: {len(findings)} unlinked TODO/FIXME/HACK marker(s):"]
    for finding in findings:
        lines.append(
            f"  {finding.path}:{finding.line}: {finding.marker} must use "
            f"# {finding.marker}(#N): explanation"
        )
    return "\n".join(lines)


def format_json(findings: list[UnlinkedTodoFinding]) -> str:
    """Render *findings* as a JSON string."""
    return json.dumps({"violations": [asdict(finding) for finding in findings]}, indent=2)


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``hephaestus-check-unlinked-todo``."""
    parser = create_validation_parser(__doc__, prog="hephaestus-check-unlinked-todo")
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Python files or directories to scan (default: hephaestus/ scripts/)",
    )
    args = parser.parse_args(argv)
    repo_root = resolve_repo_root(args)
    findings = find_unlinked_todos(repo_root, paths=args.paths or None)
    print(format_json(findings) if args.json else format_report(findings))
    return 0 if not findings else 1


if __name__ == "__main__":
    sys.exit(main())
