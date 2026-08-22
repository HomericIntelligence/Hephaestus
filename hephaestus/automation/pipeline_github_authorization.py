"""Repository-scoped GitHub reads that authorize a conditional merge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from urllib.parse import quote

from hephaestus.automation.github_api import (
    gh_call,
    merge_authorization_reviews_page_query,
)
from hephaestus.automation.merge_authorization import normalize_review_database_id

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *  # noqa: F403
from .pipeline_github_transport import _parse_included_http_response


class PipelineGitHubAuthorization(_PipelineGitHubHost):
    """Own complete native-review and collaborator-permission snapshots."""

    def _review_commit_id(self, pr_number: int, review_database_id: int) -> str:
        """Return the REST review's immutable commit ID or fail closed."""
        if self._repo_slug is None or pr_number <= 0 or review_database_id <= 0:
            raise RuntimeError("review commit requires a repo-scoped review identity")
        owner, name = self._owner_name()
        result = gh_call(
            [
                "api",
                "--method",
                "GET",
                f"repos/{owner}/{name}/pulls/{pr_number}/reviews/{review_database_id}",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("merge authorization review is unavailable")
        try:
            review = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("merge authorization review is malformed") from exc
        if not isinstance(review, dict) or review.get("id") != review_database_id:
            raise RuntimeError("merge authorization review identity is malformed")
        commit_id = review.get("commit_id")
        if not isinstance(commit_id, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_id):
            raise RuntimeError("merge authorization review commit is malformed")
        return commit_id

    def _rest_review_page(self, pr_number: int, page_number: int) -> list[object]:
        """Read one bounded REST review-list page or fail closed."""
        owner, name = self._owner_name()
        route = f"repos/{owner}/{name}/pulls/{pr_number}/reviews?per_page=100&page={page_number}"
        result = gh_call(["api", "--method", "GET", route], check=False)
        if result.returncode != 0:
            raise RuntimeError("merge authorization review list is unavailable")
        try:
            reviews = json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise RuntimeError("merge authorization review list is malformed") from exc
        if not isinstance(reviews, list) or len(reviews) > 100:
            raise RuntimeError("merge authorization review list is malformed")
        return reviews

    @staticmethod
    def _matching_rest_review_commit(
        review: object, review_node_ids: set[str]
    ) -> tuple[str, str] | None:
        """Extract one requested REST review's node-bound commit identity."""
        if not isinstance(review, Mapping):
            raise RuntimeError("merge authorization review list is malformed")
        node_id = review.get("node_id")
        if not isinstance(node_id, str) or node_id not in review_node_ids:
            return None
        commit_id = review.get("commit_id")
        if not isinstance(commit_id, str) or not re.fullmatch(r"[0-9a-f]{40}", commit_id):
            raise RuntimeError("merge authorization review commit is malformed")
        return node_id, commit_id

    def _review_commit_ids_for_node_ids(
        self, pr_number: int, review_node_ids: set[str]
    ) -> dict[str, str]:
        """Bind nullable GraphQL review database IDs through REST node IDs.

        A GraphQL review's database ID can be null; the individual REST
        endpoint accepts only its numeric ID. Its collection exposes the same
        immutable GraphQL node ID as ``node_id``. Only matching node and commit
        IDs bind; absent, duplicate, malformed, and over-bound results fail closed.
        """
        if not review_node_ids:
            return {}
        if self._repo_slug is None or pr_number <= 0:
            raise RuntimeError("review commits require repo-scoped review identities")
        commits: dict[str, str] = {}
        for page_number in range(100):
            reviews = self._rest_review_page(pr_number, page_number + 1)
            for review in reviews:
                matched = self._matching_rest_review_commit(review, review_node_ids)
                if matched is None:
                    continue
                node_id, commit_id = matched
                if node_id in commits:
                    raise RuntimeError("merge authorization review list identity is malformed")
                commits[node_id] = commit_id
            if len(reviews) < 100:
                if commits.keys() != review_node_ids:
                    raise RuntimeError("merge authorization review is unavailable")
                return commits
        raise RuntimeError("merge authorization review list exceeded its bound")

    def _bind_rest_review_commits(
        self, pr_number: int, reviews: list[dict[str, object]]
    ) -> tuple[dict[str, object], ...]:
        """Require REST and GraphQL to agree on every marked review's head."""
        nullable_node_ids: set[str] = set()
        for review in reviews:
            if review.get("body") != "<!-- hephaestus-merge-authorization:v1 -->":
                continue
            if "fullDatabaseId" not in review:
                raise RuntimeError("merge authorization review database id is unavailable")
            database_id = review.get("fullDatabaseId")
            if database_id is None:
                review_node_id = review.get("id")
                if not isinstance(review_node_id, str) or not review_node_id:
                    raise RuntimeError("merge authorization review node id is unavailable")
                nullable_node_ids.add(review_node_id)
                continue
            if isinstance(database_id, bool) or not isinstance(database_id, int):
                raise RuntimeError("merge authorization review database id is unavailable")
            commit_id = self._review_commit_id(pr_number, database_id)
            graph_commit = review.get("commit")
            if not isinstance(graph_commit, Mapping) or graph_commit.get("oid") != commit_id:
                raise RuntimeError("merge authorization review commit changed during admission")
        nullable_commits = self._review_commit_ids_for_node_ids(pr_number, nullable_node_ids)
        for review in reviews:
            if review.get("body") != "<!-- hephaestus-merge-authorization:v1 -->":
                continue
            if review.get("fullDatabaseId") is not None:
                continue
            review_node_id = review["id"]
            graph_commit = review.get("commit")
            if (
                not isinstance(review_node_id, str)
                or not isinstance(graph_commit, Mapping)
                or graph_commit.get("oid") != nullable_commits.get(review_node_id)
            ):
                raise RuntimeError("merge authorization review commit changed during admission")
        return tuple(reviews)

    def _complete_merge_authorization_review_snapshot(  # noqa: C901
        self, pr_number: int
    ) -> tuple[dict[str, object], ...]:
        """Read one complete, repository-scoped native-review traversal."""
        if self._repo_slug is None or pr_number <= 0:
            raise RuntimeError("merge authorization requires a repo-scoped positive PR")
        owner, name = self._owner_name()
        spec = merge_authorization_reviews_page_query(owner, name, pr_number)
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
                raise RuntimeError("merge authorization review connection is unavailable")
            total_count = connection.get("totalCount")
            if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
                raise RuntimeError("merge authorization review count is malformed")
            if expected_total is None:
                expected_total = total_count
            elif expected_total != total_count:
                raise RuntimeError("merge authorization review count changed during traversal")
            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise RuntimeError("merge authorization review nodes are malformed")
            for node in nodes:
                if not isinstance(node, Mapping):
                    raise RuntimeError("merge authorization review node is malformed")
                normalized = dict(node)
                review_id = normalized.get("id")
                if not isinstance(review_id, str) or not review_id:
                    raise RuntimeError("merge authorization review node id is malformed")
                if review_id in seen_review_ids:
                    raise RuntimeError("merge authorization review node id is duplicated")
                seen_review_ids.add(review_id)
                if "fullDatabaseId" in normalized:
                    with suppress(ValueError):
                        normalized["fullDatabaseId"] = normalize_review_database_id(
                            normalized["fullDatabaseId"]
                        )
                    # Preserve candidate-level malformed data for the pure
                    # resolver to classify as REPLAYED.
                reviews.append(normalized)
            page_info = connection.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(
                page_info.get("hasNextPage"), bool
            ):
                raise RuntimeError("merge authorization review page info is malformed")
            if not page_info["hasNextPage"]:
                if expected_total != len(reviews):
                    raise RuntimeError("merge authorization review traversal was truncated")
                return self._bind_rest_review_commits(pr_number, reviews)
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise RuntimeError("merge authorization review cursor loop")
            seen_cursors.add(next_cursor)
            after = next_cursor
            if page_number == 99:
                raise RuntimeError("merge authorization review traversal exceeded its bound")
        raise RuntimeError("merge authorization review traversal did not terminate")

    def merge_authorization_reviews(self, pr_number: int) -> tuple[dict[str, object], ...]:
        """Return one stable, complete native-review snapshot or raise."""
        first = self._complete_merge_authorization_review_snapshot(pr_number)
        second = self._complete_merge_authorization_review_snapshot(pr_number)
        if first != second:
            raise RuntimeError("merge authorization review snapshot changed during admission")
        return first

    def _collaborator_permission_response(
        self, login: str
    ) -> tuple[int | None, dict[str, object] | None]:
        """Fetch and parse one repository collaborator-permission response."""
        if self._repo_slug is None or not isinstance(login, str) or not login:
            raise RuntimeError("repository permission requires a repo and actor")
        owner, name = self._owner_name()
        result = gh_call(
            [
                "api",
                "--method",
                "GET",
                "--include",
                f"repos/{owner}/{name}/collaborators/{quote(login, safe='')}/permission",
            ],
            check=False,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        status, body, malformed = _parse_included_http_response(stdout)
        if status == 404:
            return status, None
        return status, None if malformed else body

    def repository_permission_for_actor(self, login: str) -> str:
        """Return the actor's current legacy repository permission."""
        status, body = self._collaborator_permission_response(login)
        if status == 404:
            return "NONE"
        if status != 200 or not isinstance(body, dict):
            raise RuntimeError("repository permission is unavailable")
        permission = body.get("permission")
        if permission not in {"none", "read", "triage", "write", "maintain", "admin"}:
            raise RuntimeError("repository permission response is malformed")
        return "WRITE" if permission == "maintain" else str(permission).upper()


__all__ = ["PipelineGitHubAuthorization"]
