"""Repository-scoped GitHub review-list queries."""

from __future__ import annotations

from collections.abc import Mapping

from hephaestus.automation.github_api import pull_request_reviews_page_query

from .pipeline_github_contract import _PipelineGitHubHost


class PipelineGitHubReviewQueries(_PipelineGitHubHost):
    """Provide complete and stable pull-request review snapshots."""

    def _complete_pull_request_review_snapshot(  # noqa: C901
        self, pr_number: int
    ) -> tuple[dict[str, object], ...]:
        """Read one complete repository-scoped review traversal."""
        if self._repo_slug is None or pr_number <= 0:
            raise RuntimeError("review query requires a repo-scoped positive PR")
        owner, name = self._owner_name()
        spec = pull_request_reviews_page_query(owner, name, pr_number)
        reviews: list[dict[str, object]] = []
        seen_cursors: set[str] = set()
        seen_review_ids: set[str] = set()
        after: str | None = None
        expected_total: int | None = None
        for page_number in range(100):
            fields: dict[str, int | str] = {"number": pr_number}
            if after is not None:
                fields["after"] = after
            pull_request = self._graphql(spec, **fields)
            connection = pull_request.get("reviews")
            if not isinstance(connection, dict):
                raise RuntimeError("pull-request review connection is unavailable")
            total_count = connection.get("totalCount")
            if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
                raise RuntimeError("pull-request review count is malformed")
            if expected_total is None:
                expected_total = total_count
            elif expected_total != total_count:
                raise RuntimeError("pull-request review count changed during traversal")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise RuntimeError("pull-request review nodes are malformed")
            for node in nodes:
                if not isinstance(node, Mapping):
                    raise RuntimeError("pull-request review node is malformed")
                review = {
                    "id": node.get("id"),
                    "body": node.get("body"),
                    "state": node.get("state"),
                    "viewerDidAuthor": node.get("viewerDidAuthor"),
                }
                review_id = review["id"]
                if not isinstance(review_id, str) or not review_id:
                    raise RuntimeError("pull-request review ID is malformed")
                if review_id in seen_review_ids:
                    raise RuntimeError("pull-request review ID is duplicated")
                seen_review_ids.add(review_id)
                reviews.append(review)
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                raise RuntimeError("pull-request review page info is malformed")
            if not page_info["hasNextPage"]:
                if expected_total != len(reviews):
                    raise RuntimeError("pull-request review traversal was truncated")
                return tuple(reviews)
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("pull-request review cursor loop")
            seen_cursors.add(next_cursor)
            after = next_cursor
            if page_number == 99:
                raise RuntimeError("pull-request review traversal exceeded its bound")
        raise RuntimeError("pull-request review traversal did not terminate")

    def pull_request_reviews(self, pr_number: int) -> tuple[dict[str, object], ...]:
        """Return one stable and complete review snapshot."""
        first = self._complete_pull_request_review_snapshot(pr_number)
        second = self._complete_pull_request_review_snapshot(pr_number)
        if first != second:
            raise RuntimeError("pull-request review snapshot changed during admission")
        return first


__all__ = ["PipelineGitHubReviewQueries"]
