#!/usr/bin/env python3
"""Compact automation-owned GitHub issue timelines to two canonical comments.

Dry-run is the default. Use ``--apply`` only after reviewing the per-issue
plan. Issue bodies, human comments, foreign comments, and pull-request
timelines are never edited by this command.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from hephaestus.automation import github_api
from hephaestus.automation.issue_timeline import (
    IssueTimelineCompaction,
    issue_comments_from_metadata,
    plan_issue_timeline_compaction,
)
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)


def _repo(value: str) -> tuple[str, str]:
    owner, separator, name = value.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise argparse.ArgumentTypeError("repository must be OWNER/NAME")
    return owner, name


def _issue_numbers(
    repo: tuple[str, str],
    *,
    state: str,
    selected: frozenset[int],
) -> list[int]:
    owner, name = repo
    result = github_api._gh_call(
        [
            "api",
            "--paginate",
            "--slurp",
            f"/repos/{owner}/{name}/issues?state={state}&per_page=100",
        ]
    )
    pages = json.loads(result.stdout or "[]")
    if not isinstance(pages, list):
        raise RuntimeError("GitHub issue listing returned a non-list response")
    records: list[dict[str, Any]] = []
    for page in pages:
        if isinstance(page, list):
            records.extend(record for record in page if isinstance(record, dict))
        elif isinstance(page, dict):
            records.append(page)
    return sorted(
        int(record["number"])
        for record in records
        if record.get("number") is not None
        and "pull_request" not in record
        and (not selected or int(record["number"]) in selected)
    )


def _plan_issue(
    issue_number: int,
    *,
    repo: tuple[str, str],
    viewer_login: str,
) -> IssueTimelineCompaction:
    metadata = github_api.fetch_issue_comments_metadata(issue_number, repo)
    return plan_issue_timeline_compaction(
        issue_comments_from_metadata(metadata, viewer_login=viewer_login)
    )


def _apply_issue(
    issue_number: int,
    plan: IssueTimelineCompaction,
    *,
    repo: tuple[str, str],
) -> None:
    if plan.plan_needs_update and plan.plan_body is not None:
        github_api.gh_issue_upsert_owned_comment(
            issue_number,
            PLAN_CANONICAL_MARKER,
            plan.plan_body,
            repo=repo,
        )
    if plan.review_needs_update and plan.review_body is not None:
        github_api.gh_issue_upsert_owned_comment(
            issue_number,
            PLAN_REVIEW_CANONICAL_MARKER,
            plan.review_body,
            repo=repo,
        )
    for comment_id in plan.delete_comment_ids:
        github_api.gh_issue_delete_comment(comment_id, repo=repo, missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    """Return the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=_repo, help="OWNER/NAME (defaults to current repo)")
    parser.add_argument("--state", choices=("all", "open", "closed"), default="all")
    parser.add_argument("--issues", nargs="*", type=int, default=())
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the displayed plan; without this flag the command is read-only",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fail-per-issue, ownership-checked compaction workflow."""
    args = build_parser().parse_args(argv)
    repo = args.repo or github_api.get_repo_info()
    viewer_login = github_api.gh_current_login() or ""
    if not viewer_login:
        print("error: authenticated GitHub login could not be verified", file=sys.stderr)
        return 2
    failures = 0
    changed = 0
    for issue_number in _issue_numbers(
        repo,
        state=args.state,
        selected=frozenset(args.issues),
    ):
        try:
            plan = _plan_issue(issue_number, repo=repo, viewer_login=viewer_login)
            if not plan.has_changes:
                continue
            changed += 1
            action = "APPLY" if args.apply else "DRY-RUN"
            print(
                f"{action} #{issue_number}: "
                f"plan_update={plan.plan_needs_update} "
                f"review_update={plan.review_needs_update} "
                f"delete={list(plan.delete_comment_ids)}"
            )
            if args.apply:
                _apply_issue(issue_number, plan, repo=repo)
                remaining = _plan_issue(issue_number, repo=repo, viewer_login=viewer_login)
                if remaining.has_changes:
                    raise RuntimeError("post-apply verification did not converge")
        except Exception as error:  # fail one issue without abandoning the inventory
            failures += 1
            print(f"ERROR #{issue_number}: {type(error).__name__}: {error}", file=sys.stderr)
    mode = "applied" if args.apply else "would change"
    print(f"summary: {changed} issue(s) {mode}; {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
