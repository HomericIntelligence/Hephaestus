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
    comment_nodes = (
        comment_connection
        if isinstance(comment_connection, list)
        else comment_connection.get("nodes", [])
        if isinstance(comment_connection, dict)
        else None
    )
    if not isinstance(comment_nodes, list):
        return None
    first_comment = comment_nodes[0] if comment_nodes else {}
    if not isinstance(first_comment, dict):
        return None
    comments: list[dict[str, str]] = []
    authors: list[str] = []
    for comment in comment_nodes:
        if not isinstance(comment, dict):
            return None
        comment_author = ""
        author_node = comment.get("author")
        if isinstance(author_node, dict):
            comment_author = author_node.get("login") or ""
        elif isinstance(author_node, str):
            comment_author = author_node
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


def _complete_thread_snapshot(  # noqa: C901 - GraphQL response validation is fail-closed
    owner: str,
    repo: str,
    pr_number: int,
    thread_id: str,
) -> dict[str, Any] | None:
    """Return every comment for one PR-local review thread.

    Review-thread connections are paginated independently of the PR's thread
    connection.  This helper deliberately completes that second pagination
    rather than dropping a long-lived conversation from an address prompt.
    """
    query = (
        "query($owner:String!,$name:String!,$number:Int!,$threadId:ID!,$after:String){"
        "repository(owner:$owner,name:$name){pullRequest(number:$number){id}}"
        "node(id:$threadId){... on PullRequestReviewThread{"
        "id isResolved path line side:diffSide pullRequest{"
        "id number repository{name owner{login}}}"
        "comments(first:100,after:$after){pageInfo{hasNextPage endCursor}"
        "nodes{body author{login}}}}}}"
    )
    comments: list[dict[str, Any]] = []
    after: str | None = None
    requested_pr_id: str | None = None
    expected_thread_fields: tuple[bool, str, int | None, str] | None = None
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
            "-F",
            f"threadId={thread_id}",
        ]
        if after is not None:
            argv.extend(["-F", f"after={after}"])
        result = _api._gh_call(argv)
        data = json.loads(result.stdout)
        _api._check_graphql_errors(
            data,
            f"review-thread snapshot(pr={pr_number}, thread={thread_id})",
        )
        data_node = data.get("data") if isinstance(data, dict) else None
        repository = data_node.get("repository") if isinstance(data_node, dict) else None
        requested_pr = repository.get("pullRequest") if isinstance(repository, dict) else None
        node = data_node.get("node") if isinstance(data_node, dict) else None
        if not isinstance(requested_pr, dict) or not isinstance(node, dict):
            return None
        pr_id = requested_pr.get("id")
        if not isinstance(pr_id, str) or not pr_id or node.get("id") != thread_id:
            return None
        if requested_pr_id is None:
            requested_pr_id = pr_id
        elif requested_pr_id != pr_id:
            return None
        pull_request = node.get("pullRequest")
        thread_repository = (
            pull_request.get("repository") if isinstance(pull_request, dict) else None
        )
        thread_owner = (
            thread_repository.get("owner") if isinstance(thread_repository, dict) else None
        )
        if (
            not isinstance(pull_request, dict)
            or pull_request.get("id") != requested_pr_id
            or pull_request.get("number") != pr_number
            or not isinstance(thread_repository, dict)
            or thread_repository.get("name") != repo
            or not isinstance(thread_owner, dict)
            or thread_owner.get("login") != owner
            or not isinstance(node.get("isResolved"), bool)
            or not isinstance(node.get("path"), str)
            or (
                node.get("line") is not None
                and (isinstance(node.get("line"), bool) or not isinstance(node.get("line"), int))
            )
            or not isinstance(node.get("side"), str)
            or not node.get("side")
        ):
            return None
        thread_fields = (node["isResolved"], node["path"], node.get("line"), node["side"])
        if expected_thread_fields is None:
            expected_thread_fields = thread_fields
        elif expected_thread_fields != thread_fields:
            return None
        connection = node.get("comments")
        comment_nodes = connection.get("nodes") if isinstance(connection, dict) else None
        page_info = connection.get("pageInfo") if isinstance(connection, dict) else None
        if not isinstance(comment_nodes, list) or not isinstance(page_info, dict):
            return None
        for comment in comment_nodes:
            if not isinstance(comment, dict):
                return None
            author_node = comment.get("author")
            author = author_node.get("login") if isinstance(author_node, dict) else ""
            body = comment.get("body")
            if not isinstance(author, str) or not isinstance(body, str):
                return None
            comments.append({"body": body, "author": author})
        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            return None
        if not has_next_page:
            if expected_thread_fields is None:
                return None
            return {
                "id": thread_id,
                "isResolved": expected_thread_fields[0],
                "path": expected_thread_fields[1],
                "line": expected_thread_fields[2],
                "side": expected_thread_fields[3],
                "comments": comments,
            }
        next_cursor = page_info.get("endCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == after:
            raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
        after = next_cursor


def gh_pr_list_unresolved_threads(  # noqa: C901 - complete thread pagination is fail-closed
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
        "        nodes{ id isResolved }"
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
        if not isinstance(nodes, list):
            raise RuntimeError("could not fetch all PR review threads")
        for node in nodes:
            if not isinstance(node, dict):
                raise RuntimeError("could not fetch all PR review threads")
            if node.get("isResolved"):
                continue
            thread_id = node.get("id")
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("could not fetch all PR review threads")
            snapshot = _complete_thread_snapshot(owner, repo, pr_number, thread_id)
            if snapshot is None:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            fact = _unresolved_thread_fact(snapshot)
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
