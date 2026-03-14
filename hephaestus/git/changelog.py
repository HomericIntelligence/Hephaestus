#!/usr/bin/env python3
"""Generate changelog from git commit history for HomericIntelligence projects.

Usage:
    python -m hephaestus.git.changelog                    # Since last tag
    python -m hephaestus.git.changelog v0.2.0             # For specific version
    python -m hephaestus.git.changelog v0.2.0 v0.1.0      # Between versions
    python -m hephaestus.git.changelog --output CHANGELOG.md
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from hephaestus.utils.helpers import get_repo_root, run_subprocess


def run_git_command(args: list[str], cwd: Path | None = None) -> str:
    """Run a git command and return output.

    Args:
        args: Git command arguments
        cwd: Working directory for command (defaults to repo root)

    Returns:
        Git command output, or empty string on failure

    """
    if cwd is None:
        cwd = get_repo_root()

    result = run_subprocess(["git", *args], cwd=str(cwd), check=False)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def get_latest_tag() -> str | None:
    """Get the most recent release tag.

    Returns:
        Latest tag name or None if no tags exist

    """
    output = run_git_command(["describe", "--tags", "--abbrev=0"])
    return output if output else None


def get_previous_tag(current_tag: str) -> str | None:
    """Get the tag before the current one.

    Args:
        current_tag: Current tag name

    Returns:
        Previous tag name or None

    """
    output = run_git_command(["describe", "--tags", "--abbrev=0", f"{current_tag}^"])
    return output if output else None


def get_commits_between(from_ref: str | None, to_ref: str = "HEAD") -> list[str]:
    """Get commit messages between two refs.

    Args:
        from_ref: Starting ref (tag/commit)
        to_ref: Ending ref (defaults to HEAD)

    Returns:
        List of commit lines

    """
    range_spec = f"{from_ref}..{to_ref}" if from_ref else to_ref

    output = run_git_command(
        [
            "log",
            range_spec,
            "--pretty=format:%h|%s|%an",
            "--no-merges",
        ]
    )

    if not output:
        return []

    return output.split("\n")


def parse_commit(commit_line: str) -> tuple[str, str, str, str]:
    """Parse a commit line into (hash, type, scope, message).

    Handles conventional commits format: type(scope): message

    Args:
        commit_line: Commit line in format "hash|subject|author"

    Returns:
        Tuple of (hash, type, scope, message)

    """
    parts = commit_line.split("|", 2)
    if len(parts) != 3:
        return ("", "other", "", commit_line)

    commit_hash, subject, _author = parts

    # Parse conventional commit format
    commit_type = "other"
    scope = ""
    message = subject

    if ":" in subject:
        prefix, rest = subject.split(":", 1)
        message = rest.strip()

        # Extract type and optional scope
        if "(" in prefix and ")" in prefix:
            commit_type = prefix.split("(")[0].strip().lower()
            scope = prefix.split("(")[1].split(")")[0].strip()
        else:
            commit_type = prefix.strip().lower()

    return (commit_hash, commit_type, scope, message)


def categorize_commits(commits: list[str]) -> dict[str, list[tuple[str, str, str]]]:
    """Categorize commits by type.

    Args:
        commits: List of commit lines

    Returns:
        Dict mapping category name to list of (hash, scope, message) tuples

    """
    categories = defaultdict(list)

    type_to_category = {
        "feat": "Features",
        "fix": "Bug Fixes",
        "perf": "Performance",
        "docs": "Documentation",
        "refactor": "Refactoring",
        "test": "Testing",
        "ci": "CI/CD",
        "chore": "Maintenance",
        "build": "Build",
        "style": "Style",
    }

    for commit_line in commits:
        if not commit_line.strip():
            continue

        commit_hash, commit_type, scope, message = parse_commit(commit_line)

        category = type_to_category.get(commit_type, "Other")
        categories[category].append((commit_hash, scope, message))

    return dict(categories)


def generate_changelog(
    version: str,
    from_ref: str | None = None,
    to_ref: str = "HEAD",
) -> str:
    """Generate changelog content.

    Args:
        version: Version string for the release (e.g., "v0.2.0")
        from_ref: Starting ref (tag/commit), defaults to previous tag
        to_ref: Ending ref, defaults to HEAD

    Returns:
        Formatted changelog as markdown string

    """
    lines = []

    # Header
    lines.append(f"# Changelog for {version}")
    lines.append("")
    lines.append(f"**Release Date**: {datetime.now().strftime('%Y-%m-%d')}")
    lines.append("")

    # Determine range
    if from_ref is None:
        from_ref = get_previous_tag(version) if version != "HEAD" else get_latest_tag()

    # Get commits
    commits = get_commits_between(from_ref, to_ref)

    if not commits:
        lines.append("No changes recorded.")
        return "\n".join(lines)

    # Categorize
    categories = categorize_commits(commits)

    # Priority order for categories
    category_order = [
        "Features",
        "Bug Fixes",
        "Performance",
        "Documentation",
        "Refactoring",
        "Testing",
        "CI/CD",
        "Build",
        "Maintenance",
        "Style",
        "Other",
    ]

    # Output categories
    for category in category_order:
        if category not in categories:
            continue

        commits_in_category = categories[category]
        if not commits_in_category:
            continue

        lines.append(f"## {category}")
        lines.append("")

        for commit_hash, scope, message in commits_in_category:
            scope_text = f"**{scope}**: " if scope else ""
            lines.append(f"- {scope_text}{message} ({commit_hash})")

        lines.append("")

    return "\n".join(lines)


def main() -> None:
    """Generate a changelog from git commit history."""
    parser = argparse.ArgumentParser(description="Generate changelog from git commit history")
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Version for changelog (defaults to latest tag or 'Unreleased')",
    )
    parser.add_argument(
        "from_ref",
        nargs="?",
        default=None,
        help="Starting ref (defaults to previous tag)",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file (defaults to stdout)",
    )
    parser.add_argument(
        "--to",
        default="HEAD",
        help="Ending ref (default: HEAD)",
    )

    args = parser.parse_args()

    # Determine version
    version = args.version
    if not version:
        latest = get_latest_tag()
        version = latest if latest else "Unreleased"

    # Generate changelog
    changelog = generate_changelog(version, args.from_ref, args.to)

    # Output
    if args.output:
        output_path = Path(args.output)
        output_path.write_text(changelog)
        print(f"Changelog written to {output_path}")
    else:
        print(changelog)

    sys.exit(0)


if __name__ == "__main__":
    main()
