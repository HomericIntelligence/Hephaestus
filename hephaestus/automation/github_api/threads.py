"""Pull-request review thread helpers."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

import hephaestus.automation.github_api as _api


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
        "query($owner:String!,$name:String!,$number:Int!){"
        "  repository(owner:$owner,name:$name){"
        "    pullRequest(number:$number){"
        "      reviewThreads(first:100){"
        "        nodes{ id isResolved path line side:diffSide "
        "comments(first:20){ nodes{ body author{ login } } } }"
        "      }"
        "    }"
        "  }"
        "}"
    )

    result = _api._gh_call(
        [
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
    )
    data = json.loads(result.stdout)
    _api._check_graphql_errors(data, f"gh_pr_list_unresolved_threads(pr={pr_number})")

    nodes = (
        data.get("data", {})
        .get("repository", {})
        .get("pullRequest", {})
        .get("reviewThreads", {})
        .get("nodes", [])
    )

    threads: list[dict[str, Any]] = []
    for node in nodes:
        if node.get("isResolved"):
            continue
        comment_nodes = node.get("comments", {}).get("nodes", [])
        first_comment = comment_nodes[0] if comment_nodes else {}
        body = first_comment.get("body", "")
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
        author = authors[0] if authors else ""
        threads.append(
            {
                "id": node["id"],
                "path": node.get("path", ""),
                "line": node.get("line"),
                "side": node.get("side") or "RIGHT",
                "body": body,
                "author": author,
                "authors": authors,
                "comments": comments,
            }
        )

    _api.logger.debug("Found %s unresolved thread(s) on PR #%s", len(threads), pr_number)
    return threads


def gh_pr_resolve_thread(
    thread_id: str,
    reply_body: str | None = None,
    dry_run: bool = False,
) -> None:
    """Resolve a PR review thread, optionally adding a reply first.

    Args:
        thread_id: GraphQL node ID of the review thread
        reply_body: Optional reply comment text. When omitted, the thread is
            resolved without adding another review comment.
        dry_run: If True, log intent without posting

    """
    if dry_run:
        if reply_body:
            _api.logger.info(
                "[dry_run] Would resolve thread %r with reply: %r", thread_id, reply_body
            )
        else:
            _api.logger.info("[dry_run] Would resolve thread %r without reply", thread_id)
        return

    if reply_body:
        # Step 1: post a reply to the thread via GraphQL addPullRequestReviewThreadReply.
        # NOTE (#999): the deprecated ``addPullRequestReviewComment`` input type has no
        # ``pullRequestReviewThreadId`` field, so it failed on every call. The reply-to-
        # thread surface is ``addPullRequestReviewThreadReply``, whose input accepts
        # ``pullRequestReviewThreadId`` + ``body``.
        reply_mutation = """
mutation AddReply($threadId: ID!, $body: String!) {
  addPullRequestReviewThreadReply(input: {pullRequestReviewThreadId: $threadId, body: $body}) {
    comment { id }
  }
}
"""
        reply_result = _api._gh_call(
            [
                "api",
                "graphql",
                "-f",
                f"query={reply_mutation}",
                "-f",
                f"threadId={thread_id}",
                "-f",
                f"body={reply_body}",
            ]
        )
        # JSONDecodeError is benign here — only an explicit ``errors`` array must
        # surface. ``contextlib.suppress`` lets the helper raise on errors while
        # silently absorbing the no-body case.
        with contextlib.suppress(json.JSONDecodeError):
            _api._check_graphql_errors(
                json.loads(reply_result.stdout or "{}"),
                f"gh_pr_resolve_thread.reply(thread={thread_id})",
            )

    # Resolve the thread.
    resolve_mutation = """
mutation ResolveThread($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) {
    thread { id isResolved }
  }
}
"""
    resolve_result = _api._gh_call(
        [
            "api",
            "graphql",
            "-f",
            f"query={resolve_mutation}",
            "-f",
            f"threadId={thread_id}",
        ]
    )
    with contextlib.suppress(json.JSONDecodeError):
        _api._check_graphql_errors(
            json.loads(resolve_result.stdout or "{}"),
            f"gh_pr_resolve_thread.resolve(thread={thread_id})",
        )
    _api.logger.info("Resolved review thread %r", thread_id)
