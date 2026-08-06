"""Tests for shared GitHub GraphQL connection pagination."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.github_api.pagination import (
    collect_graphql_connection_nodes,
)


def test_collects_all_pages_and_forwards_cursors() -> None:
    """Aggregate page nodes and pass each returned cursor to the next fetch."""
    calls: list[str | None] = []
    pages: dict[str | None, dict[str, Any]] = {
        None: {
            "nodes": [{"id": "T1"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        },
        "cursor-1": {
            "nodes": [{"id": "T2"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        },
    }

    def fetch_page(after: str | None) -> dict[str, Any]:
        calls.append(after)
        return pages[after]

    assert collect_graphql_connection_nodes(fetch_page, connection_name="threads") == [
        {"id": "T1"},
        {"id": "T2"},
    ]
    assert calls == [None, "cursor-1"]


def test_stops_when_has_next_page_is_false() -> None:
    """Do not issue a request after a terminal page."""
    calls: list[str | None] = []

    def fetch_page(after: str | None) -> dict[str, Any]:
        calls.append(after)
        return {"nodes": [], "pageInfo": {"hasNextPage": False, "endCursor": "ignored"}}

    assert collect_graphql_connection_nodes(fetch_page, connection_name="threads") == []
    assert calls == [None]


@pytest.mark.parametrize(
    "page_info",
    [
        None,
        {"hasNextPage": "yes", "endCursor": None},
        {"hasNextPage": True, "endCursor": None},
    ],
)
def test_rejects_malformed_page_info(page_info: Any) -> None:
    """Reject missing, mistyped, or incomplete page metadata."""
    with pytest.raises(RuntimeError, match="threads"):
        collect_graphql_connection_nodes(
            lambda _after: {"nodes": [], "pageInfo": page_info},
            connection_name="threads",
        )


def test_rejects_repeated_cursor() -> None:
    """Reject a cursor that repeats instead of looping indefinitely."""
    calls: list[str | None] = []

    def fetch_page(after: str | None) -> dict[str, Any]:
        calls.append(after)
        return {
            "nodes": [{"id": after or "T1"}],
            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
        }

    with pytest.raises(RuntimeError, match="repeated threads cursor"):
        collect_graphql_connection_nodes(fetch_page, connection_name="threads")
    assert calls == [None, "cursor-1"]


def test_rejects_connection_above_safety_ceiling() -> None:
    """Reject a later page that would make the collection exceed its ceiling."""
    pages = iter(
        [
            {"nodes": [{"id": "T1"}], "pageInfo": {"hasNextPage": True, "endCursor": "next"}},
            {"nodes": [{"id": "T2"}], "pageInfo": {"hasNextPage": False, "endCursor": None}},
        ]
    )

    with pytest.raises(RuntimeError, match="exceeds the 1-node safety ceiling"):
        collect_graphql_connection_nodes(
            lambda _after: next(pages),
            connection_name="threads",
            max_nodes=1,
        )
