"""Pull-request review thread helpers."""

from __future__ import annotations

import json
import re
from typing import Any

import hephaestus.automation.github_api as _api


def _unresolved_thread_fact(node: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize one complete unresolved GraphQL thread into a host fact."""
    if node.get("isResolved"):
        return None
    comment_connection = node.get("comments", {})
    if comment_connection.get("pageInfo", {}).get("hasNextPage"):
        raise RuntimeError(f"could not fetch all comments for PR review thread {node.get('id')}")
    comment_nodes = comment_connection.get("nodes", [])
    first_comment = comment_nodes[0] if comment_nodes else {}
    comments: list[dict[str, str]] = []
    authors: list[str] = []
    for comment in comment_nodes:
        comment_author = ""
        author_node = comment.get("author")
        if isinstance(author_node, dict):
            comment_author = author_node.get("login") or ""
        if comment_author:
            authors.append(comment_author)
        comments.append({"body": comment.get("body") or "", "author": comment_author})
    return {
        "id": node["id"],
        "path": node.get("path", ""),
        "line": node.get("line"),
        "side": node.get("side") or "RIGHT",
        "body": first_comment.get("body", ""),
        "author": authors[0] if authors else "",
        "authors": authors,
        "comments": comments,
    }


def gh_pr_list_unresolved_threads(
    pr_number: int,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """List unresolved review threads for a PR.

    Args:
        pr_number: PR number
        dry_run: If True, return empty list

    Returns:
        List of thread dicts with keys: id (str), path (str), line (int | None),
        side (str), body (str), author (str — the first comment's author login,
        ``""`` if unknown), authors (list[str]), comments (list[dict]).

    """
    if dry_run:
        _api.logger.info("[dry_run] Would list unresolved threads for PR #%s", pr_number)
        return []

    owner, repo = _api.get_repo_info()

    # Sanitize owner/repo to prevent injection (same pattern as prefetch_issue_states)
    if not re.match(r"^[a-zA-Z0-9_-]+$", owner) or not re.match(r"^[a-zA-Z0-9_-]+$", repo):
        _api.logger.error("Invalid owner/repo format: %s/%s", owner, repo)
        return []

    query = (
        "query($owner:String!,$name:String!,$number:Int!,$after:String){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100,after:$after){"
        "        pageInfo{ hasNextPage endCursor }"
        "        nodes{ id isResolved path line side:diffSide "
        "comments(first:20){ pageInfo{ hasNextPage } "
        "nodes{ body author{ login } } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )

    threads: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        argv = [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-F",
            f"owner={owner}",
            "-F",
            f"name={repo}",
            "-F",
            f"number={int(pr_number)}",
        ]
        if after is not None:
            argv.extend(["-F", f"after={after}"])
        result = _api._gh_call(argv)
        data = json.loads(result.stdout)
        _api._check_graphql_errors(data, f"gh_pr_list_unresolved_threads(pr={pr_number})")
        review_threads = (
            data.get("data", {})
            .get("repository", {})
            .get("pullRequest", {})
            .get("reviewThreads", {})
        )
        nodes = review_threads.get("nodes", [])
        for node in nodes:
            fact = _unresolved_thread_fact(node)
            if fact is not None:
                threads.append(fact)
        page_info = review_threads.get("pageInfo", {})
        if not page_info.get("hasNextPage"):
            break
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == after:
            raise RuntimeError("could not fetch all PR review threads")
        after = next_cursor

    _api.logger.debug("Found %s unresolved thread(s) on PR #%s", len(threads), pr_number)
    return threads
