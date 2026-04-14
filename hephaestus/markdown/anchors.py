"""Validate anchor fragments in markdown links against actual headings.

Converts markdown heading text to GitHub-style anchor slugs and checks that
every fragment (``#section-name``) in links pointing to a target file resolves
to a real heading in that file.

Usage::

    hephaestus-validate-anchors
    hephaestus-validate-anchors --target docs/installation.md
    hephaestus-validate-anchors README.md docs/guide.md --target docs/installation.md

Exit codes:
    0  All anchor links are valid (or no anchor links found)
    1  One or more broken anchor links found
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def heading_to_anchor(heading: str) -> str:
    """Convert markdown heading text to a GitHub-style anchor slug.

    GitHub's algorithm:
    1. Lowercase the text.
    2. Replace spaces with hyphens.
    3. Remove all characters except ``[a-z0-9-]``.
    4. Collapse consecutive hyphens and strip leading/trailing ones.

    Args:
        heading: Heading text (without leading ``#`` characters and whitespace).

    Returns:
        The anchor slug (without the leading ``#``).

    """
    slug = heading.lower()
    slug = slug.replace(" ", "-")
    slug = re.sub(r"[^a-z0-9\-]", "", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def extract_headings(content: str) -> list[str]:
    """Extract all heading texts from markdown content.

    Args:
        content: Full markdown file content.

    Returns:
        List of heading texts (without the leading ``#`` characters), in
        document order.

    """
    headings: list[str] = []
    for line in content.splitlines():
        m = re.match(r"^#{1,6}\s+(.*)", line)
        if m:
            headings.append(m.group(1).strip())
    return headings


def extract_anchored_links(
    content: str,
    source_path: str,
    target_basename: str | None = None,
) -> list[tuple[str, str, str]]:
    """Extract links with anchor fragments from markdown content.

    If *target_basename* is given, only links whose base path ends with that
    filename are returned.  If ``None``, all links containing an anchor
    fragment are returned.

    Args:
        content: Full markdown file content.
        source_path: Path of the source file (used only for reporting).
        target_basename: Filename to filter on (e.g. ``"installation.md"``).
            Pass ``None`` to capture all anchored links.

    Returns:
        List of ``(source_path, link_target, anchor)`` tuples where *anchor*
        is the fragment text without the leading ``#``.

    """
    results: list[tuple[str, str, str]] = []
    link_re = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")

    for line in content.splitlines():
        for m in link_re.finditer(line):
            target = m.group(2).strip()
            base, _, fragment = target.partition("#")
            if not fragment:
                continue
            if target_basename is not None and not base.endswith(target_basename):
                continue
            results.append((source_path, target, fragment))

    return results


def _collect_markdown_files(repo_root: Path) -> list[Path]:
    """Return all markdown files under *repo_root* (excluding build dirs).

    Args:
        repo_root: Repository root directory.

    Returns:
        List of markdown file paths.

    """
    exclude = {".pixi", "build", "dist", ".git", "worktrees"}
    return [p for p in repo_root.rglob("*.md") if not any(part in exclude for part in p.parts)]


def validate_anchors(
    source_files: list[Path],
    target_file: Path,
    target_basename: str | None = None,
) -> list[str]:
    """Validate anchor fragments in *source_files* that point to *target_file*.

    Args:
        source_files: Markdown files to scan for links.
        target_file: The file whose headings define the valid anchors.
        target_basename: Filter links by this basename (default: ``target_file.name``).

    Returns:
        List of human-readable error messages.  Empty list means all valid.

    """
    if not target_file.exists():
        return [f"Target file not found: {target_file}"]

    basename = target_basename or target_file.name
    target_content = target_file.read_text(encoding="utf-8")
    headings = extract_headings(target_content)
    valid_anchors = {heading_to_anchor(h) for h in headings}

    errors: list[str] = []
    for source_path in source_files:
        if not source_path.exists():
            errors.append(f"Source file not found: {source_path}")
            continue
        content = source_path.read_text(encoding="utf-8")
        links = extract_anchored_links(content, str(source_path), basename)
        for src, link_target, anchor in links:
            if anchor not in valid_anchors:
                errors.append(
                    f"{src}: broken anchor '#{anchor}' in link '{link_target}' "
                    f"(valid: {sorted(valid_anchors)})"
                )
    return errors


def check_anchors(
    target_file: Path,
    source_files: list[Path] | None = None,
    repo_root: Path | None = None,
    verbose: bool = False,
) -> int:
    """Public API: validate anchors and return an exit code.

    If *source_files* is ``None``, all markdown files under *repo_root* are
    scanned.  If *repo_root* is also ``None``, it is auto-detected via git.

    Args:
        target_file: File whose headings define the valid anchors.
        source_files: Files to scan.  ``None`` = scan whole repo.
        repo_root: Repository root for auto-discovery.  ``None`` = auto-detect.
        verbose: Print a success message when no errors are found.

    Returns:
        0 if all anchor links are valid, 1 otherwise.

    """
    if source_files is None:
        if repo_root is None:
            from hephaestus.utils.helpers import get_repo_root

            repo_root = get_repo_root()
        source_files = _collect_markdown_files(repo_root)

    errors = validate_anchors(source_files, target_file)

    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        print(f"\n{len(errors)} broken anchor link(s) found.", file=sys.stderr)
        return 1

    if verbose:
        print(f"All anchor links to {target_file.name} are valid.")
    return 0


def main() -> int:
    """CLI entry point for anchor validation.

    Returns:
        Exit code (0 if all anchors valid, 1 otherwise).

    """
    parser = argparse.ArgumentParser(
        description="Validate anchor fragments in markdown links against actual headings",
        epilog="Example: %(prog)s --target docs/installation.md --verbose",
    )
    parser.add_argument(
        "sources",
        nargs="*",
        type=Path,
        help="Markdown files to scan for links (default: all .md files in repo)",
    )
    parser.add_argument(
        "--target",
        type=Path,
        required=True,
        help="Target markdown file whose headings define valid anchors",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root for auto-discovery (default: auto-detect via git)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print success message when no errors are found",
    )

    args = parser.parse_args()

    repo_root: Path | None = args.repo_root
    source_files: list[Path] | None = args.sources if args.sources else None

    return check_anchors(
        target_file=args.target,
        source_files=source_files,
        repo_root=repo_root,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    sys.exit(main())
