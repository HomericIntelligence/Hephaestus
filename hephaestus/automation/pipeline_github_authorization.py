"""Repository-scoped GitHub reads that authorize a conditional merge."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from contextlib import suppress
from urllib.parse import quote

from hephaestus.automation.merge_authorization import normalize_review_database_id

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_transport import *  # noqa: F403


class PipelineGitHubAuthorization(_PipelineGitHubHost):
    """Own complete native-review and collaborator-permission snapshots."""

    def _complete_merge_authorization_review_snapshot(  # noqa: C901
        self, pr_number: int
    ) -> tuple[dict[str, object], ...]:
        """Read one complete, repository-scoped native-review traversal."""
        if self._repo_slug is None or pr_number <= 0:
            raise RuntimeError("merge authorization requires a repo-scoped positive PR")
        query = (
            "query($owner:String!,$name:String!,$number:Int!,$after:String){"
            " repository(owner:$owner,name:$name){"
            "  id name owner{login}"
            "  pullRequest(number:$number){"
            "   id number headRefOid"
            "   reviews(first:100,after:$after){"
            "    totalCount pageInfo{hasNextPage endCursor}"
            "    nodes{id fullDatabaseId body state submittedAt updatedAt "
            "includesCreatedEdit lastEditedAt viewerDidAuthor "
            "author{login __typename} commit{oid}}"
            "   }"
            "  }"
            " }"
            "}"
        )
        reviews: list[dict[str, object]] = []
        seen_cursors: set[str] = set()
        seen_review_ids: set[str] = set()
        after: str | None = None
        expected_total: int | None = None
        for page_number in range(100):
            fields: dict[str, int | str] = {"number": pr_number}
            if after is not None:
                fields["after"] = after
            response = self._graphql(query, **fields)
            if not isinstance(response, Mapping):
                raise RuntimeError("merge authorization GraphQL response is unavailable")
            errors = response.get("errors")
            if errors not in (None, []):
                raise RuntimeError("merge authorization GraphQL response contains errors")
            data = response.get("data") if isinstance(response, dict) else None
            repository = data.get("repository") if isinstance(data, dict) else None
            if not isinstance(repository, dict):
                raise RuntimeError("merge authorization repository envelope is unavailable")
            if (
                not isinstance(repository.get("id"), str)
                or repository.get("name") != self.repo
                or not isinstance(repository.get("owner"), dict)
                or repository["owner"].get("login") != self.org
            ):
                raise RuntimeError("merge authorization repository identity is malformed")
            pull_request = repository.get("pullRequest")
            if not isinstance(pull_request, dict):
                raise RuntimeError("merge authorization pull-request is unavailable")
            if (
                not isinstance(pull_request.get("id"), str)
                or pull_request.get("number") != pr_number
                or not isinstance(pull_request.get("headRefOid"), str)
                or not pull_request["headRefOid"]
            ):
                raise RuntimeError("merge authorization pull-request identity is malformed")
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
                return tuple(reviews)
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
        result = self._gh(
            [
                "api",
                "--method",
                "GET",
                "--include",
                f"/repos/{owner}/{name}/collaborators/{quote(login, safe='')}/permission",
            ],
            check=False,
        )
        stdout = result.stdout if isinstance(result.stdout, str) else ""
        matches = list(re.finditer(r"^HTTP/\S+\s+(\d{3})\b", stdout, re.MULTILINE))
        if not matches:
            return None, None
        status = int(matches[-1].group(1))
        body_start = stdout.find("\n\n", matches[-1].start())
        if body_start < 0:
            body_start = stdout.find("\r\n\r\n", matches[-1].start())
            separator_length = 4
        else:
            separator_length = 2
        if body_start < 0:
            return status, None
        try:
            body = json.loads(stdout[body_start + separator_length :].strip())
        except json.JSONDecodeError:
            return status, None
        return status, body if isinstance(body, dict) else None

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
