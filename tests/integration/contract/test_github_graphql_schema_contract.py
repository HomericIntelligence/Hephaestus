"""Authenticated, read-only GraphQL schema contract coverage."""

from __future__ import annotations

import pytest

from hephaestus.automation.github_api.graphql import (
    github_schema_contract_query,
    run_graphql,
)

pytestmark = [pytest.mark.integration, pytest.mark.contract]


def test_graphql_schema_contains_automation_contract(gh_authenticated: None) -> None:
    """Verify selected query, mutation, payload, and receipt types exist."""
    schema = run_graphql(github_schema_contract_query(), {})["__schema"]
    assert isinstance(schema, dict)
    assert schema["queryType"]["name"] == "Query"
    assert schema["mutationType"]["name"] == "Mutation"

    types = {entry["name"]: entry for entry in schema["types"]}
    for name in (
        "Repository",
        "Issue",
        "PullRequest",
        "PullRequestReviewThread",
        "PullRequestReviewComment",
        "PullRequestReview",
        "AddPullRequestReviewThreadReplyPayload",
        "AddPullRequestReviewPayload",
        "SubmitPullRequestReviewPayload",
        "ResolveReviewThreadPayload",
        "UpdatePullRequestReviewCommentPayload",
    ):
        assert name in types

    def field_names(type_name: str, key: str = "fields") -> set[str]:
        return {field["name"] for field in types[type_name].get(key) or []}

    assert {"owner", "name", "issue", "pullRequest"} <= field_names("Repository")
    assert {"number", "comments", "labels", "title", "state"} <= field_names("Issue")
    assert {"id", "number", "reviewThreads", "state", "headRefOid"} <= field_names("PullRequest")
    assert {"id", "isResolved", "path", "line", "diffSide", "comments"} <= field_names(
        "PullRequestReviewThread"
    )
    assert {"id", "body", "viewerDidAuthor", "pullRequestReview"} <= field_names(
        "PullRequestReviewComment"
    )
    assert {"id", "state", "commit", "pullRequest"} <= field_names("PullRequestReview")
    assert {"clientMutationId", "comment"} <= field_names("AddPullRequestReviewThreadReplyPayload")
    assert {"clientMutationId", "pullRequestReview"} <= field_names("AddPullRequestReviewPayload")
    assert {"clientMutationId", "pullRequestReview"} <= field_names(
        "SubmitPullRequestReviewPayload"
    )
    assert {"clientMutationId", "thread"} <= field_names("ResolveReviewThreadPayload")
    assert {"clientMutationId", "pullRequestReviewComment"} <= field_names(
        "UpdatePullRequestReviewCommentPayload"
    )
