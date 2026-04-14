"""GitHub contribution statistics via the ``gh`` CLI.

Fetches and displays issue, PR, and commit counts for a date range.  Uses
``gh api`` subprocess calls instead of PyGithub so no token management is
required beyond a working ``gh auth`` session.

Usage::

    hephaestus-github-stats 2026-01-01 2026-01-31
    hephaestus-github-stats 2026-01-01 2026-01-31 --author mvillmow
    hephaestus-github-stats 2026-01-01 2026-01-31 --repo owner/repo
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Data helpers
# ---------------------------------------------------------------------------


def validate_date(date_string: str) -> bool:
    """Validate that a string matches the ``YYYY-MM-DD`` format.

    Args:
        date_string: Candidate date string.

    Returns:
        True if the string is a valid ISO date, False otherwise.

    """
    try:
        datetime.strptime(date_string, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def get_current_repo() -> str:
    """Return the current repository name in ``owner/repo`` format via ``gh``.

    Returns:
        Repository name string.

    Raises:
        SystemExit: With code 1 if ``gh repo view`` fails.

    """
    result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"Error: Failed to get repo name: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Stats collectors
# ---------------------------------------------------------------------------


def get_issues_stats(
    start_date: str, end_date: str, author: str | None, repo: str
) -> dict[str, int]:
    """Return issue counts (total / open / closed) for the given date range.

    Args:
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.
        author: Optional GitHub username to filter by.
        repo: Repository in ``owner/repo`` format.

    Returns:
        Dict with keys ``"total"``, ``"open"``, ``"closed"``.

    """
    base_parts = [f"repo:{repo}", "type:issue", f"created:{start_date}..{end_date}"]
    if author:
        base_parts.append(f"author:{author}")
    base_query = " ".join(base_parts)

    result = subprocess.run(
        ["gh", "api", "search/issues", "-f", f"q={base_query}", "--jq", ".total_count"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"total": 0, "open": 0, "closed": 0}

    total = int(result.stdout.strip())

    result_open = subprocess.run(
        [
            "gh",
            "api",
            "search/issues",
            "-f",
            f"q={base_query} state:open",
            "--jq",
            ".total_count",
        ],
        capture_output=True,
        text=True,
    )
    open_count = int(result_open.stdout.strip()) if result_open.returncode == 0 else 0

    return {"total": total, "open": open_count, "closed": total - open_count}


def get_prs_stats(
    start_date: str, end_date: str, author: str | None, repo: str
) -> dict[str, int]:
    """Return PR counts (total / merged / open / closed) for the given date range.

    Args:
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.
        author: Optional GitHub username to filter by.
        repo: Repository in ``owner/repo`` format.

    Returns:
        Dict with keys ``"total"``, ``"merged"``, ``"open"``, ``"closed"``.

    """
    base_parts = [f"repo:{repo}", "type:pr", f"created:{start_date}..{end_date}"]
    if author:
        base_parts.append(f"author:{author}")
    base_query = " ".join(base_parts)

    result = subprocess.run(
        ["gh", "api", "search/issues", "-f", f"q={base_query}", "--jq", ".total_count"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"total": 0, "merged": 0, "open": 0, "closed": 0}

    total = int(result.stdout.strip())

    result_merged = subprocess.run(
        [
            "gh",
            "api",
            "search/issues",
            "-f",
            f"q={base_query} is:merged",
            "--jq",
            ".total_count",
        ],
        capture_output=True,
        text=True,
    )
    merged = int(result_merged.stdout.strip()) if result_merged.returncode == 0 else 0

    result_open = subprocess.run(
        [
            "gh",
            "api",
            "search/issues",
            "-f",
            f"q={base_query} state:open",
            "--jq",
            ".total_count",
        ],
        capture_output=True,
        text=True,
    )
    open_count = int(result_open.stdout.strip()) if result_open.returncode == 0 else 0

    return {
        "total": total,
        "merged": merged,
        "open": open_count,
        "closed": total - merged - open_count,
    }


def get_commits_stats(
    start_date: str, end_date: str, author: str | None, repo: str
) -> dict[str, int]:
    """Return commit count for the given date range.

    Args:
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.
        author: Optional GitHub username to filter by.
        repo: Repository in ``owner/repo`` format.

    Returns:
        Dict with key ``"total"``.

    """
    owner, repo_name = repo.split("/", 1)

    params = [
        "gh",
        "api",
        f"repos/{owner}/{repo_name}/commits",
        "--paginate",
        "-f",
        f"since={start_date}T00:00:00Z",
        "-f",
        f"until={end_date}T23:59:59Z",
        "--jq",
        "length",
    ]
    if author:
        params += ["-f", f"author={author}"]

    result = subprocess.run(params, capture_output=True, text=True)
    if result.returncode != 0:
        return {"total": 0}

    total = sum(
        int(line.strip()) for line in result.stdout.strip().split("\n") if line.strip()
    )
    return {"total": total}


def collect_stats(
    start_date: str, end_date: str, author: str | None, repo: str
) -> dict[str, Any]:
    """Collect all stats (issues, PRs, commits) for the given date range.

    Args:
        start_date: Start date in ``YYYY-MM-DD`` format.
        end_date: End date in ``YYYY-MM-DD`` format.
        author: Optional GitHub username to filter by.
        repo: Repository in ``owner/repo`` format.

    Returns:
        Dict with keys ``"issues"``, ``"prs"``, ``"commits"``.

    """
    return {
        "issues": get_issues_stats(start_date, end_date, author, repo),
        "prs": get_prs_stats(start_date, end_date, author, repo),
        "commits": get_commits_stats(start_date, end_date, author, repo),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def format_stats_table(stats: dict[str, Any]) -> str:
    """Render a text table from the collected stats dict.

    Args:
        stats: Dict with ``"issues"``, ``"prs"``, ``"commits"`` keys.

    Returns:
        Multi-line string with formatted table.

    """
    sep = "=" * 60
    lines = [
        sep,
        "GitHub Contribution Statistics",
        sep,
        "",
        "ISSUES",
        "-" * 60,
        f"  Total:  {stats['issues']['total']:>6}",
        f"  Open:   {stats['issues']['open']:>6}",
        f"  Closed: {stats['issues']['closed']:>6}",
        "",
        "PULL REQUESTS",
        "-" * 60,
        f"  Total:  {stats['prs']['total']:>6}",
        f"  Merged: {stats['prs']['merged']:>6}",
        f"  Open:   {stats['prs']['open']:>6}",
        f"  Closed: {stats['prs']['closed']:>6}",
        "",
        "COMMITS",
        "-" * 60,
        f"  Total:  {stats['commits']['total']:>6}",
        "",
        sep,
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """CLI entry point for GitHub contribution statistics.

    Returns:
        Exit code: 0 for success, 1 for invalid arguments or errors.

    """
    parser = argparse.ArgumentParser(
        description="Get GitHub contribution statistics",
        epilog=(
            "Examples:\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31 --author mvillmow\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31 --repo owner/repo"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("start_date", help="Start date (YYYY-MM-DD)")
    parser.add_argument("end_date", help="End date (YYYY-MM-DD)")
    parser.add_argument("--author", help="Filter by author username")
    parser.add_argument(
        "--repo", help="Repository (owner/repo), defaults to current repo"
    )

    args = parser.parse_args()

    if not validate_date(args.start_date):
        print(f"Error: Invalid start date format: {args.start_date}", file=sys.stderr)
        print("Expected format: YYYY-MM-DD", file=sys.stderr)
        return 1

    if not validate_date(args.end_date):
        print(f"Error: Invalid end date format: {args.end_date}", file=sys.stderr)
        print("Expected format: YYYY-MM-DD", file=sys.stderr)
        return 1

    repo = args.repo if args.repo else get_current_repo()

    print(f"Repository: {repo}")
    print(f"Date range: {args.start_date} to {args.end_date}")
    if args.author:
        print(f"Author: {args.author}")
    print()
    print("Fetching statistics...")

    stats = collect_stats(args.start_date, args.end_date, args.author, repo)
    print(format_stats_table(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
