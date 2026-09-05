"""Stable pull-request review-query tests."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.github_api as github_api
import hephaestus.automation.pipeline_github as pg
import hephaestus.automation.pipeline_github_scope_expansion as scope_expansion_mod


def _review(review_id: str = "R1", *, body: str = "review") -> dict[str, object]:
    return {
        "id": review_id,
        "body": body,
        "state": "COMMENTED",
        "viewerDidAuthor": True,
    }


def _pull_request(
    nodes: list[dict[str, object]],
    *,
    total_count: int,
    has_next_page: bool = False,
    end_cursor: str | None = None,
) -> dict[str, object]:
    return {
        "id": "pr-node",
        "number": 7,
        "reviews": {
            "totalCount": total_count,
            "pageInfo": {
                "hasNextPage": has_next_page,
                "endCursor": end_cursor,
            },
            "nodes": nodes,
        },
    }


def _install_pages(
    adapter: pg.PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    pages: Iterator[dict[str, object]],
    calls: list[dict[str, int | str]] | None = None,
) -> None:
    def graphql(_spec: object, **fields: int | str) -> dict[str, object]:
        if calls is not None:
            calls.append(fields)
        return next(pages)

    monkeypatch.setattr(adapter, "_graphql", graphql)


def test_review_query_uses_only_scope_expansion_fields() -> None:
    """The generic query does not request retired authorization fields."""
    spec = github_api.pull_request_reviews_page_query("org", "repo", 7)

    assert "nodes{id body state viewerDidAuthor}" in spec.query
    for retired in (
        "fullDatabaseId",
        "submittedAt",
        "updatedAt",
        "includesCreatedEdit",
        "lastEditedAt",
        "author{",
        "commit{",
    ):
        assert retired not in spec.query


@pytest.mark.parametrize(
    "node",
    [
        {"id": "", "body": "review", "state": "COMMENTED", "viewerDidAuthor": True},
        {"id": "R1", "body": None, "state": "COMMENTED", "viewerDidAuthor": True},
        {"id": "R1", "body": "review", "state": None, "viewerDidAuthor": True},
        {"id": "R1", "body": "review", "state": "COMMENTED", "viewerDidAuthor": "yes"},
    ],
    ids=("empty-id", "missing-body", "missing-state", "invalid-owner"),
)
def test_review_query_rejects_malformed_reduced_fields(node: dict[str, object]) -> None:
    """Each reduced review field has a validated GraphQL type."""
    spec = github_api.pull_request_reviews_page_query("org", "repo", 7)
    data = {
        "repository": {
            "id": "repo-node",
            "name": "repo",
            "owner": {"login": "org"},
            "pullRequest": _pull_request([node], total_count=1),
        }
    }

    with pytest.raises(ValueError, match="review node"):
        spec.validate(data)


def test_complete_review_pagination_is_read_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query returns one complete stable snapshot."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    first = _pull_request([_review()], total_count=2, has_next_page=True, end_cursor="c1")
    second = _pull_request([_review("R2")], total_count=2)
    calls: list[dict[str, int | str]] = []
    _install_pages(adapter, monkeypatch, iter([first, second, first, second]), calls)

    reviews = adapter.pull_request_reviews(7)

    assert reviews == (_review(), _review("R2"))
    assert calls == [
        {"number": 7},
        {"number": 7, "after": "c1"},
        {"number": 7},
        {"number": 7, "after": "c1"},
    ]


def test_scope_expansion_publication_uses_generic_review_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic review snapshot prevents duplicate blocking reviews."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    marker = "<!-- hephaestus-scope-expansion:test -->"
    body = f"{marker}\nBlocked by a child issue."
    review = {"id": "R2", "body": body, "state": "COMMENTED", "viewerDidAuthor": True}
    reviews = MagicMock(side_effect=[(), (review,)])
    monkeypatch.setattr(adapter, "pull_request_reviews", reviews)
    call_mock = MagicMock(
        return_value=SimpleNamespace(returncode=0, stdout='{"id":2,"node_id":"R2"}')
    )
    monkeypatch.setattr(scope_expansion_mod, "direct_gh_call", call_mock)

    assert adapter.post_scope_expansion_blocking_review(7, body=body, marker=marker) == "R2"
    assert reviews.call_count == 2
    call_mock.assert_called_once()

    reviews.reset_mock(side_effect=True)
    reviews.side_effect = None
    reviews.return_value = (review,)
    call_mock.reset_mock()

    assert adapter.post_scope_expansion_blocking_review(7, body=body, marker=marker) == "R2"
    call_mock.assert_not_called()


def test_changed_review_snapshot_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A review change between complete reads is unavailable."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    _install_pages(
        adapter,
        monkeypatch,
        iter(
            [
                _pull_request([_review()], total_count=1),
                _pull_request([_review(body="changed")], total_count=1),
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="snapshot changed"):
        adapter.pull_request_reviews(7)


def test_duplicate_review_ids_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A page overlap cannot create an ambiguous review snapshot."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    _install_pages(
        adapter,
        monkeypatch,
        iter(
            [
                _pull_request([_review()], total_count=2, has_next_page=True, end_cursor="c1"),
                _pull_request([_review()], total_count=2),
            ]
        ),
    )

    with pytest.raises(RuntimeError, match="duplicated"):
        adapter.pull_request_reviews(7)


@pytest.mark.parametrize(
    ("pages", "message"),
    [
        ([_pull_request([_review()], total_count=2)], "truncated"),
        (
            [
                _pull_request([_review()], total_count=2, has_next_page=True, end_cursor="c1"),
                _pull_request([_review("R2")], total_count=2, has_next_page=True, end_cursor="c1"),
            ],
            "cursor loop",
        ),
        (
            [
                _pull_request([_review()], total_count=2, has_next_page=True, end_cursor="c1"),
                _pull_request([_review("R2")], total_count=3),
            ],
            "count changed",
        ),
    ],
    ids=("truncated", "cursor-loop", "count-change"),
)
def test_incomplete_review_traversal_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    pages: list[dict[str, object]],
    message: str,
) -> None:
    """Count and cursor faults cannot produce a partial snapshot."""
    adapter = pg.PipelineGitHub("org", repo="repo")
    _install_pages(adapter, monkeypatch, iter(pages))

    with pytest.raises(RuntimeError, match=message):
        adapter.pull_request_reviews(7)


@pytest.mark.parametrize("pr_number", [0, -1])
def test_review_query_requires_positive_repository_scoped_pr(
    pr_number: int,
) -> None:
    """The query rejects an invalid pull-request identity before transport."""
    adapter = pg.PipelineGitHub("org", repo="repo")

    with pytest.raises(RuntimeError, match="positive PR"):
        adapter.pull_request_reviews(pr_number)
