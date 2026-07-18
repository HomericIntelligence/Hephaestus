#!/usr/bin/env python3
"""Reject private/PII tokens in tracked or staged text files.

This guard merges two denylist sources at the repository root, both one fixed
string per line:

* ``.heph-project-denylist`` — committed and centrally enforced, so the policy
  is effective for every contributor and in CI even when no local file exists.
  Only patterns safe to name in a public repo belong here.
* ``.heph-private-denylist`` — optional, operator-local, and gitignored, for
  genuine secret values known only on a given machine.

When any token is present, this guard scans supplied paths (working-tree mode),
git-tracked files, or staged index content and fails if a denylisted string
appears. The denylist files themselves are never flagged as their own
violation on any scan path. Diagnostics intentionally print only
source/path/line, never the matched value or source line.

Usage:
    python scripts/check_private_denylist.py [--staged] [--tracked] [paths...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Literal, NamedTuple

DENYLIST_FILENAME = ".heph-private-denylist"
PROJECT_DENYLIST_FILENAME = ".heph-project-denylist"
PRIVATE_DENYLIST_REDACTION = "<redacted-private-denylist-value>"
ScanSource = Literal["working-tree", "tracked", "staged"]


class Finding(NamedTuple):
    """One denylist match in a text file."""

    source: ScanSource
    path: Path
    line_number: int


def get_repo_root() -> Path:
    """Return the repository root by walking up to the nearest ``pyproject.toml``."""
    path = Path(__file__).resolve().parent
    while path != path.parent:
        if (path / "pyproject.toml").exists():
            return path
        path = path.parent
    return Path(__file__).resolve().parent.parent


def _is_denylist_file(repo_root: Path, path: Path) -> bool:
    """True if *path* is either denylist file (never scanned as a violation)."""
    candidate = path if path.is_absolute() else repo_root / path
    denylist_paths = {
        (repo_root / DENYLIST_FILENAME).resolve(),
        (repo_root / PROJECT_DENYLIST_FILENAME).resolve(),
    }
    return candidate.resolve() in denylist_paths


def _read_tokens(path: Path) -> list[str]:
    """Return denylist tokens from *path*, ignoring blank lines and comments."""
    if not path.exists():
        return []
    tokens: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if token and not token.startswith("#"):
            tokens.append(token)
    return tokens


def load_denylist(repo_root: Path) -> list[str]:
    """Return merged project + local denylist tokens, project-first, de-duplicated.

    Merges the committed, centrally-enforced ``.heph-project-denylist`` with the
    optional operator-local ``.heph-private-denylist`` so the policy is effective
    even when no local file exists. Order is stable and duplicates are removed.
    """
    merged: list[str] = []
    seen: set[str] = set()
    for filename in (PROJECT_DENYLIST_FILENAME, DENYLIST_FILENAME):
        for token in _read_tokens(repo_root / filename):
            if token not in seen:
                seen.add(token)
                merged.append(token)
    return merged


def _git_paths(repo_root: Path, cmd: list[str]) -> list[Path]:
    """Return null-delimited git path output as relative paths."""
    result = subprocess.run(cmd, cwd=repo_root, capture_output=True, check=True)
    return [Path(raw.decode()) for raw in result.stdout.split(b"\0") if raw]


def tracked_files(repo_root: Path, pathspecs: list[str] | None = None) -> list[Path]:
    """Return git-tracked files for manual scans."""
    cmd = ["git", "ls-files", "-z", "--", *(pathspecs or [])]
    return [repo_root / path for path in _git_paths(repo_root, cmd)]


def staged_files(repo_root: Path, pathspecs: list[str] | None = None) -> list[Path]:
    """Return staged paths from the git index without reading the worktree."""
    cmd = [
        "git",
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--diff-filter=ACMR",
        "--",
        *(pathspecs or []),
    ]
    return _git_paths(repo_root, cmd)


def staged_text(repo_root: Path, rel_path: Path) -> str | None:
    """Return staged UTF-8 text for *rel_path*, or None for non-text blobs."""
    result = subprocess.run(
        ["git", "show", f":{rel_path.as_posix()}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    )
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _relative(repo_root: Path, path: Path) -> Path:
    try:
        return path.resolve().relative_to(repo_root.resolve())
    except ValueError:
        return path


def _scan_text(source: ScanSource, rel_path: Path, text: str, tokens: list[str]) -> list[Finding]:
    """Return redacted findings for denylist tokens in text content."""
    findings: list[Finding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if any(token in line for token in tokens):
            findings.append(Finding(source, rel_path, line_number))
    return findings


def _redact_private_tokens(text: str, tokens: list[str]) -> str:
    """Replace local denylist values before emitting diagnostics."""
    redacted = text
    for token in sorted((token for token in tokens if token), key=len, reverse=True):
        redacted = redacted.replace(token, PRIVATE_DENYLIST_REDACTION)
    return redacted


def scan_paths(
    repo_root: Path,
    paths: list[Path],
    tokens: list[str],
    *,
    source: ScanSource = "working-tree",
) -> list[Finding]:
    """Return denylist matches in text files under *paths*."""
    findings: list[Finding] = []
    if not tokens:
        return findings
    for path in paths:
        candidate = path if path.is_absolute() else repo_root / path
        if _is_denylist_file(repo_root, candidate) or not candidate.is_file():
            continue
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        except OSError:
            continue
        rel_path = _relative(repo_root, candidate)
        findings.extend(_scan_text(source, rel_path, "\n".join(lines), tokens))
    return findings


def main(argv: list[str] | None = None) -> int:
    """Fail (exit 1) if a scanned text file contains a local denylist token."""
    raw_args = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracked", action="store_true", help="scan tracked working-tree text")
    parser.add_argument("--staged", action="store_true", help="scan staged index text")
    parser.add_argument("paths", nargs="*", help="optional pathspecs or working-tree paths")
    if any(arg in ("--help", "-h") for arg in raw_args):
        parser.print_help()
        return 0
    args = parser.parse_args(raw_args)

    repo_root = get_repo_root()
    tokens = load_denylist(repo_root)
    if not tokens:
        return 0
    pathspecs = list(args.paths)
    findings: list[Finding] = []

    if not args.tracked and not args.staged and pathspecs:
        findings.extend(scan_paths(repo_root, [Path(p) for p in pathspecs], tokens))
    else:
        if not args.tracked and not args.staged:
            args.tracked = True
        if args.tracked:
            findings.extend(
                scan_paths(
                    repo_root,
                    tracked_files(repo_root, pathspecs),
                    tokens,
                    source="tracked",
                )
            )
        if args.staged:
            for rel_path in staged_files(repo_root, pathspecs):
                if _is_denylist_file(repo_root, rel_path):
                    continue
                text = staged_text(repo_root, rel_path)
                if text is not None:
                    findings.extend(_scan_text("staged", rel_path, text, tokens))

    if not findings:
        return 0

    print("ERROR: private denylist match(es) found. Remove the value before committing:")
    for finding in findings:
        redacted_path = _redact_private_tokens(str(finding.path), tokens)
        print(f"  {finding.source} {redacted_path}:{finding.line_number}")
    print("\nMatched values and line contents are intentionally not printed.")
    print(
        f"\nDenylist sources: {PROJECT_DENYLIST_FILENAME} (project, tracked), "
        f"{DENYLIST_FILENAME} (local, untracked)"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
