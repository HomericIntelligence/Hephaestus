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
import json
import subprocess
import sys
from datetime import datetime
from typing import Any

from hephaestus.cli.localization import text
from hephaestus.cli.utils import (
    add_github_throttle_args,
    add_json_arg,
    add_version_arg,
    configure_github_throttle_from_args,
    emit_json_status,
    format_output,
)
from hephaestus.github.client import gh_call

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
    result = gh_call(
        ["repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
        check=False,
    )
    if result.returncode != 0:
        print(
            text("Error: Failed to get repo name: %(value0)s", value0=result.stderr),
            file=sys.stderr,
        )
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

    result = gh_call(
        ["api", "search/issues", "-f", f"q={base_query}", "--jq", ".total_count"],
        check=False,
    )
    if result.returncode != 0:
        return {"total": 0, "open": 0, "closed": 0}

    total = int(result.stdout.strip())

    result_open = gh_call(
        [
            "api",
            "search/issues",
            "-f",
            f"q={base_query} state:open",
            "--jq",
            ".total_count",
        ],
        check=False,
    )
    open_count = int(result_open.stdout.strip()) if result_open.returncode == 0 else 0

    return {"total": total, "open": open_count, "closed": total - open_count}


def get_prs_stats(start_date: str, end_date: str, author: str | None, repo: str) -> dict[str, int]:
    """Return PR counts (total / merged / open / closed) for the given date range.

    Issues a single ``gh api graphql`` call with aliased ``search`` fields so all
    three counts (total, merged, open) come back in one round-trip instead of
    three serial REST calls (#811). Null GraphQL fields are coerced to 0 by jq
    before parsing, and any decode failure returns the all-zero dict to preserve
    the prior degraded-mode contract.

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

    # Single batched GraphQL query (one API call) for total/merged/open counts,
    # routed through the shared gh_call adapter for circuit-breaker + rate-limit
    # protection instead of a bare subprocess.run.
    graphql_query = (
        "query($qTotal: String!, $qMerged: String!, $qOpen: String!) {"
        "  total: search(query: $qTotal, type: ISSUE, first: 0) { issueCount }"
        "  merged: search(query: $qMerged, type: ISSUE, first: 0) { issueCount }"
        "  open: search(query: $qOpen, type: ISSUE, first: 0) { issueCount }"
        "}"
    )

    result = gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={graphql_query}",
            "-f",
            f"qTotal={base_query}",
            "-f",
            f"qMerged={base_query} is:merged",
            "-f",
            f"qOpen={base_query} state:open",
            "--jq",
            (
                "[.data.total.issueCount // 0,"
                " .data.merged.issueCount // 0,"
                " .data.open.issueCount // 0]"
            ),
        ],
        check=False,
    )
    if result.returncode != 0:
        return {"total": 0, "merged": 0, "open": 0, "closed": 0}

    try:
        counts = json.loads(result.stdout.strip())
        total, merged, open_count = (int(counts[0]), int(counts[1]), int(counts[2]))
    except (ValueError, TypeError, IndexError, json.JSONDecodeError):
        return {"total": 0, "merged": 0, "open": 0, "closed": 0}

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

    try:
        result = gh_call(params[1:])
    except subprocess.CalledProcessError:
        return {"total": 0}

    total = sum(int(line.strip()) for line in result.stdout.strip().split("\n") if line.strip())
    return {"total": total}


def collect_stats(start_date: str, end_date: str, author: str | None, repo: str) -> dict[str, Any]:
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
        text("GitHub Contribution Statistics"),
        sep,
        "",
        text("ISSUES"),
        "-" * 60,
        text("  Total:  %(count)6d", count=stats["issues"]["total"]),
        text("  Open:   %(count)6d", count=stats["issues"]["open"]),
        text("  Closed: %(count)6d", count=stats["issues"]["closed"]),
        "",
        text("PULL REQUESTS"),
        "-" * 60,
        text("  Total:  %(count)6d", count=stats["prs"]["total"]),
        text("  Merged: %(count)6d", count=stats["prs"]["merged"]),
        text("  Open:   %(count)6d", count=stats["prs"]["open"]),
        text("  Closed: %(count)6d", count=stats["prs"]["closed"]),
        "",
        text("COMMITS"),
        "-" * 60,
        text("  Total:  %(count)6d", count=stats["commits"]["total"]),
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
        description=text("Get GitHub contribution statistics"),
        epilog=text(
            "Examples:\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31 --author mvillmow\n"
            "  hephaestus-github-stats 2026-01-01 2026-01-31 --repo owner/repo"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("start_date", help=text("Start date (YYYY-MM-DD)"))
    parser.add_argument("end_date", help=text("End date (YYYY-MM-DD)"))
    parser.add_argument("--author", help=text("Filter by author username"))
    parser.add_argument("--repo", help=text("Repository (owner/repo), defaults to current repo"))
    add_github_throttle_args(parser)
    add_json_arg(parser)
    add_version_arg(parser)

    args = parser.parse_args()
    configure_github_throttle_from_args(args)

    if not validate_date(args.start_date):
        if args.json:
            emit_json_status(1, message=f"Invalid start date format: {args.start_date}")
        else:
            print(
                text("Error: Invalid start date format: %(value0)s", value0=args.start_date),
                file=sys.stderr,
            )
            print(text("Expected format: YYYY-MM-DD"), file=sys.stderr)
        return 1

    if not validate_date(args.end_date):
        if args.json:
            emit_json_status(1, message=f"Invalid end date format: {args.end_date}")
        else:
            print(
                text("Error: Invalid end date format: %(value0)s", value0=args.end_date),
                file=sys.stderr,
            )
            print(text("Expected format: YYYY-MM-DD"), file=sys.stderr)
        return 1

    repo = args.repo if args.repo else get_current_repo()

    if not args.json:
        print(text("Repository: %(value0)s", value0=repo))
        print(
            text(
                "Date range: %(value0)s to %(value1)s", value0=args.start_date, value1=args.end_date
            )
        )
        if args.author:
            print(text("Author: %(value0)s", value0=args.author))
        print()
        print(text("Fetching statistics..."))

    stats = collect_stats(args.start_date, args.end_date, args.author, repo)
    if args.json:
        payload = {
            "repo": repo,
            "start_date": args.start_date,
            "end_date": args.end_date,
            "author": args.author,
            "stats": stats,
        }
        print(format_output(payload, "json"))
    else:
        print(format_stats_table(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
