"""Pull-request review thread helpers."""

from __future__ import annotations

import json
import re
from typing import Any

import hephaestus.automation.github_api as _api

from .pagination import (
    MAX_PR_REVIEW_THREAD_COMMENTS,
    collect_graphql_connection_nodes,
)


def _unresolved_thread_fact(  # noqa: C901 - malformed GraphQL facts fail closed
    node: dict[str, Any],
) -> dict[str, Any] | None:
    """Normalize one complete unresolved GraphQL thread into a host fact."""
    is_resolved = node.get("isResolved")
    thread_id = node.get("id")
    if not isinstance(is_resolved, bool) or not isinstance(thread_id, str) or not thread_id:
        return None
    if is_resolved:
        return None
    comment_connection = node.get("comments", {})
    comment_nodes = (
        comment_connection
        if isinstance(comment_connection, list)
        else comment_connection.get("nodes", [])
        if isinstance(comment_connection, dict)
        else None
    )
    if not isinstance(comment_nodes, list) or not comment_nodes:
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
        if "author" not in comment:
            return None
        author_node = comment.get("author")
        if isinstance(author_node, dict):
            author_login = author_node.get("login")
            if not isinstance(author_login, str):
                return None
            comment_author = author_login
        elif isinstance(author_node, str):
            comment_author = author_node
        elif author_node is not None:
            return None
        if comment_author:
            authors.append(comment_author)
        body = comment.get("body")
        if not isinstance(body, str):
            return None
        comments.append({"body": body, "author": comment_author})
    return {
        "id": thread_id,
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
        "nodes{id body author{login}}}}}}"
    )

    def read_once() -> tuple[dict[str, Any], bool] | None:  # noqa: C901
        page_count = 0
        requested_pr_id: str | None = None
        expected_thread_fields: tuple[bool, str, int | None, str | None] | None = None

        def fetch_comment_page(after: str | None) -> dict[str, Any]:
            nonlocal expected_thread_fields, page_count, requested_pr_id
            page_count += 1
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
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            pr_id = requested_pr.get("id")
            if not isinstance(pr_id, str) or not pr_id or node.get("id") != thread_id:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            if requested_pr_id is None:
                requested_pr_id = pr_id
            elif requested_pr_id != pr_id:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            pull_request = node.get("pullRequest")
            thread_pr_number = (
                pull_request.get("number") if isinstance(pull_request, dict) else None
            )
            thread_repository = (
                pull_request.get("repository") if isinstance(pull_request, dict) else None
            )
            thread_owner = (
                thread_repository.get("owner") if isinstance(thread_repository, dict) else None
            )
            if (
                not isinstance(pull_request, dict)
                or pull_request.get("id") != requested_pr_id
                or isinstance(thread_pr_number, bool)
                or not isinstance(thread_pr_number, int)
                or thread_pr_number != pr_number
                or not isinstance(thread_repository, dict)
                or thread_repository.get("name") != repo
                or not isinstance(thread_owner, dict)
                or thread_owner.get("login") != owner
                or not isinstance(node.get("isResolved"), bool)
                or not isinstance(node.get("path"), str)
                or (
                    node.get("line") is not None
                    and (
                        isinstance(node.get("line"), bool) or not isinstance(node.get("line"), int)
                    )
                )
                or (node.get("side") is not None and not isinstance(node.get("side"), str))
            ):
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            thread_fields = (
                node["isResolved"],
                node["path"],
                node.get("line"),
                node.get("side"),
            )
            if expected_thread_fields is None:
                expected_thread_fields = thread_fields
            elif expected_thread_fields != thread_fields:
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            connection = node.get("comments")
            if not isinstance(connection, dict):
                raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
            return connection

        try:
            comment_nodes = collect_graphql_connection_nodes(
                fetch_comment_page,
                connection_name=f"comments for PR review thread {thread_id}",
                max_nodes=MAX_PR_REVIEW_THREAD_COMMENTS,
            )
        except (_api.GitHubRateLimitError, _api.GitHubUnavailableError):
            raise
        except RuntimeError as exc:
            raise RuntimeError(
                f"could not fetch all comments for PR review thread {thread_id}: {exc}"
            ) from exc

        comments: list[dict[str, Any]] = []
        seen_comment_ids: set[str] = set()
        for comment in comment_nodes:
            if "author" not in comment:
                return None
            author_node = comment.get("author")
            if author_node is None:
                author = ""
            elif isinstance(author_node, dict):
                author_login = author_node.get("login")
                if not isinstance(author_login, str):
                    return None
                author = author_login
            else:
                return None
            comment_id = comment.get("id")
            body = comment.get("body")
            if (
                not isinstance(comment_id, str)
                or not comment_id
                or not isinstance(author, str)
                or not isinstance(body, str)
            ):
                return None
            if comment_id in seen_comment_ids:
                return None
            seen_comment_ids.add(comment_id)
            comments.append({"id": comment_id, "body": body, "author": author})

        if expected_thread_fields is None:
            return None
        return (
            {
                "id": thread_id,
                "isResolved": expected_thread_fields[0],
                "path": expected_thread_fields[1],
                "line": expected_thread_fields[2],
                "side": expected_thread_fields[3],
                "comments": comments,
            },
            page_count > 1,
        )

    first = read_once()
    if first is None:
        return None
    snapshot, was_paginated = first
    if not was_paginated:
        return snapshot
    second = read_once()
    if second is None or snapshot != second[0]:
        return None
    return second[0]


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

    def read_thread_ids() -> tuple[str, ...]:
        """Read one complete unresolved-thread traversal without hydrating it."""

        def fetch_thread_page(after: str | None) -> dict[str, Any]:
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
            data_node = data.get("data") if isinstance(data, dict) else None
            repository = data_node.get("repository") if isinstance(data_node, dict) else None
            pull_request = repository.get("pullRequest") if isinstance(repository, dict) else None
            review_threads = (
                pull_request.get("reviewThreads") if isinstance(pull_request, dict) else None
            )
            if not isinstance(review_threads, dict):
                raise RuntimeError("could not fetch all PR review threads")
            return review_threads

        try:
            nodes = collect_graphql_connection_nodes(
                fetch_thread_page,
                connection_name=f"review threads for PR #{pr_number}",
            )
        except (_api.GitHubRateLimitError, _api.GitHubUnavailableError):
            raise
        except RuntimeError as exc:
            raise RuntimeError("could not fetch all PR review threads") from exc

        thread_ids: list[str] = []
        seen: set[str] = set()
        for node in nodes:
            is_resolved = node.get("isResolved")
            thread_id = node.get("id")
            if (
                not isinstance(is_resolved, bool)
                or not isinstance(thread_id, str)
                or not thread_id
                or thread_id in seen
            ):
                raise RuntimeError("could not fetch all PR review threads")
            seen.add(thread_id)
            if not is_resolved:
                thread_ids.append(thread_id)
        return tuple(thread_ids)

    first_ids = read_thread_ids()
    if first_ids != read_thread_ids():
        raise RuntimeError("could not stabilize all PR review threads")
    threads: list[dict[str, Any]] = []
    for thread_id in first_ids:
        snapshot = _complete_thread_snapshot(owner, repo, pr_number, thread_id)
        if snapshot is None:
            raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")
        fact = _unresolved_thread_fact(snapshot)
        if fact is not None:
            threads.append(fact)
        elif snapshot.get("isResolved") is not True:
            # The outer traversal established this as unresolved.  Do not
            # silently lose it if its hydrated snapshot lacks the comment
            # history needed for an agent to investigate it.
            raise RuntimeError(f"could not fetch all comments for PR review thread {thread_id}")

    _api.logger.debug("Found %s unresolved thread(s) on PR #%s", len(threads), pr_number)
    return threads
