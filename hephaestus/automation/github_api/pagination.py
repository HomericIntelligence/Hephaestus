"""Shared GraphQL connection pagination for automation GitHub reads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

MAX_PR_REVIEW_THREAD_COMMENTS = 2_000

GraphQLPageFetcher = Callable[[str | None], dict[str, Any]]


def collect_graphql_connection_nodes(
    fetch_page: GraphQLPageFetcher,
    *,
    connection_name: str,
    max_nodes: int | None = None,
) -> list[dict[str, Any]]:
    """Collect every node in a GraphQL connection.

    The callback is invoked with the cursor for the page to fetch, or ``None``
    for the first page.  Pagination errors and safety-ceiling violations raise
    before any partial collection is returned.

    Args:
        fetch_page: Fetch one connection page for the supplied cursor.
        connection_name: Name used in validation error messages.
        max_nodes: Optional maximum number of nodes to collect.

    Returns:
        All connection nodes in page order.

    Raises:
        RuntimeError: If the connection shape, cursor chain, or node ceiling is
            invalid.

    """
    nodes: list[dict[str, Any]] = []
    seen_cursors: set[str] = set()
    after: str | None = None

    while True:
        connection = fetch_page(after)
        if not isinstance(connection, dict):
            raise RuntimeError(f"malformed {connection_name} connection")
        page_nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(page_nodes, list) or not all(
            isinstance(node, dict) for node in page_nodes
        ):
            raise RuntimeError(f"malformed {connection_name} nodes")
        if not isinstance(page_info, dict):
            raise RuntimeError(f"malformed {connection_name} pageInfo")
        if max_nodes is not None and len(nodes) + len(page_nodes) > max_nodes:
            raise RuntimeError(f"{connection_name} exceeds the {max_nodes}-node safety ceiling")
        nodes.extend(page_nodes)

        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise RuntimeError(f"malformed {connection_name} hasNextPage")
        if not has_next_page:
            return nodes

        end_cursor = page_info.get("endCursor")
        if not isinstance(end_cursor, str) or not end_cursor or end_cursor in seen_cursors:
            raise RuntimeError(f"invalid or repeated {connection_name} cursor")
        seen_cursors.add(end_cursor)
        after = end_cursor
