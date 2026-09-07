"""Contract tests for the automation GraphQL execution boundary."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.github_api.graphql import (
    GraphQLDeterministicError,
    GraphQLMutationIntent,
    GraphQLMutationOutcomeUnknownError,
    GraphQLQuerySpec,
    GraphQLResponseError,
    GraphQLRetryableError,
    ReviewCommentNotEditableError,
    enqueue_pull_request_mutation,
    pull_request_queue_entry_query,
    run_graphql,
    update_review_comment_mutation,
)
from hephaestus.github.client import (
    GitHubUnavailableError,
)


def completed(
    *,
    stdout: str = '{"data":{"ok":true}}',
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Build a completed ``gh`` result for the transport seam."""
    return subprocess.CompletedProcess(
        ["gh", "api", "graphql"],
        returncode,
        stdout=stdout,
        stderr=stderr,
    )


def query_spec() -> GraphQLQuerySpec[dict[str, object]]:
    """Return a minimal valid query spec for classifier tests."""
    return GraphQLQuerySpec(
        operation="testQuery",
        query="query testQuery { viewer { login } }",
        validate=lambda data: (
            data if data.get("ok") is True else (_ for _ in ()).throw(ValueError("missing ok"))
        ),
    )


def test_valid_query_uses_one_non_sleeping_transport_attempt() -> None:
    """A valid response is returned and the exact no-retry controls are passed."""
    raw_call = Mock(return_value=completed())
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        raw_call,
    ):
        assert run_graphql(query_spec(), {}) == {"ok": True}

    raw_call.assert_called_once()
    args, kwargs = raw_call.call_args
    assert args[0][:3] == ["api", "graphql", "-f"]
    assert kwargs == {
        "check": False,
        "retry_on_rate_limit": False,
        "max_retries": 1,
        "log_on_error": False,
        "throttle": False,
    }


def test_explicit_transport_seam_receives_only_contract_arguments() -> None:
    """A supplied raw seam is not polluted with façade-only control flags."""
    raw_call = Mock(return_value=completed())
    assert run_graphql(query_spec(), {}, call=raw_call) == {"ok": True}
    assert raw_call.call_args.kwargs == {
        "check": False,
        "retry_on_rate_limit": False,
        "max_retries": 1,
        "log_on_error": False,
        "throttle": False,
    }


def test_mutation_factory_owns_fresh_correlation_id_and_hides_body() -> None:
    """Mutation intent is executor-owned and safe summaries never contain bodies."""
    first = update_review_comment_mutation("COMMENT", "secret body")
    second = update_review_comment_mutation("COMMENT", "secret body")
    assert first.variables["id"] == "COMMENT"
    assert "clientMutationId" not in first.variables
    assert "secret body" not in first.query
    assert "secret body" not in second.query

    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            side_effect=[
                completed(
                    stdout=json.dumps(
                        {
                            "data": {
                                "updatePullRequestReviewComment": {
                                    "clientMutationId": "first-id",
                                    "pullRequestReviewComment": {
                                        "id": "COMMENT",
                                        "body": "secret body",
                                    },
                                }
                            }
                        }
                    )
                ),
                completed(
                    stdout=json.dumps(
                        {
                            "data": {
                                "updatePullRequestReviewComment": {
                                    "clientMutationId": "second-id",
                                    "pullRequestReviewComment": {
                                        "id": "COMMENT",
                                        "body": "secret body",
                                    },
                                }
                            }
                        }
                    )
                ),
            ],
        ) as raw_call,
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            side_effect=[Mock(hex="first-id"), Mock(hex="second-id")],
        ),
    ):
        run_graphql(first)
        run_graphql(second)

    first_query = " ".join(raw_call.call_args_list[0].args[0])
    second_query = " ".join(raw_call.call_args_list[1].args[0])
    assert "clientMutationId=first-id" in first_query
    assert "clientMutationId=second-id" in second_query
    prepared_intent = GraphQLMutationIntent(
        operation=first.operation,
        client_mutation_id="correlation",
        targets=(("id", "COMMENT"),),
        content_hashes=(("body", "hash"),),
    )
    assert "secret body" not in prepared_intent.safe_summary()


def test_enqueue_pull_request_sends_expected_head_and_accepts_minimal_receipt() -> None:
    """Queue admission sends the exact head and accepts stable receipt fields."""
    spec = enqueue_pull_request_mutation("PR_node", "a" * 40)
    assert spec.query.count("{") == spec.query.count("}")
    response = {
        "data": {
            "enqueuePullRequest": {
                "clientMutationId": "queue-id",
                "mergeQueueEntry": {
                    "id": "MQE_node",
                    "state": "QUEUED",
                },
            }
        }
    }
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(stdout=json.dumps(response)),
        ) as raw_call,
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
    ):
        receipt = run_graphql(spec)

    assert receipt["id"] == "MQE_node"
    request = " ".join(raw_call.call_args.args[0])
    assert "expectedHeadOid=" + "a" * 40 in request
    assert "enablePullRequestAutoMerge" not in request


@pytest.mark.parametrize(
    ("client_mutation_id", "entry"),
    [
        ("queue-id", {"id": "", "state": "QUEUED"}),
        ("queue-id", {"id": "MQE_node", "state": "UNKNOWN"}),
        ("wrong-id", {"id": "MQE_node", "state": "QUEUED"}),
    ],
    ids=("empty-entry-id", "unknown-state", "wrong-correlation"),
)
def test_enqueue_pull_request_rejects_invalid_minimal_receipt(
    client_mutation_id: str,
    entry: dict[str, str],
) -> None:
    """Queue admission rejects an invalid stable receipt field."""
    spec = enqueue_pull_request_mutation("PR_node", "a" * 40)
    response = {
        "data": {
            "enqueuePullRequest": {
                "clientMutationId": client_mutation_id,
                "mergeQueueEntry": entry,
            }
        }
    }
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(stdout=json.dumps(response)),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(GraphQLMutationOutcomeUnknownError),
    ):
        run_graphql(spec)


def test_enqueue_already_queued_is_an_outcome_unknown_error() -> None:
    """GitHub's already-queued mutation error retains its exact safe class text."""
    spec = enqueue_pull_request_mutation("PR_node", "a" * 40)
    response = {
        "data": {"enqueuePullRequest": None},
        "errors": [
            {
                "type": "UNPROCESSABLE",
                "message": "Pull request is already in the queue",
            }
        ],
    }
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(stdout=json.dumps(response)),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(
            GraphQLMutationOutcomeUnknownError,
            match=r"^Pull request is already in the queue$",
        ),
    ):
        run_graphql(spec)


def test_pull_request_queue_entry_query_binds_identity_and_head() -> None:
    """Queue readback returns one validated pull request and queue entry."""
    spec = pull_request_queue_entry_query("org", "repo", 7)
    response = {
        "data": {
            "repository": {
                "owner": {"login": "org"},
                "name": "repo",
                "pullRequest": {
                    "id": "PR_node",
                    "number": 7,
                    "state": "OPEN",
                    "headRefOid": "a" * 40,
                    "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
                },
            }
        }
    }
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        return_value=completed(stdout=json.dumps(response)),
    ):
        result = run_graphql(spec, {"owner": "org", "name": "repo", "number": 7})

    assert result["headRefOid"] == "a" * 40
    assert result["mergeQueueEntry"] == {"id": "MQE_node", "state": "AWAITING_CHECKS"}


def test_operation_kind_is_structural() -> None:
    """A query spec cannot smuggle a mutation document through the boundary."""
    with pytest.raises(ValueError, match="root operation must be query"):
        GraphQLQuerySpec(
            operation="wrong",
            query="mutation wrong { viewer { login } }",
            validate=lambda data: data,
        )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("", GraphQLDeterministicError),
        ("not json", GraphQLDeterministicError),
        ("[]", GraphQLDeterministicError),
        ('{"data":null}', GraphQLDeterministicError),
        ('{"data":{}}', GraphQLDeterministicError),
        ('{"data":{},"errors":null}', GraphQLDeterministicError),
        ('{"data":{},"errors":[]}', GraphQLDeterministicError),
        ('{"data":{},"errors":[{"message":""}]}', GraphQLDeterministicError),
    ],
)
def test_malformed_query_responses_fail_closed(
    data: str,
    expected: type[GraphQLResponseError],
) -> None:
    """Malformed envelopes never become an empty successful result."""
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        return_value=completed(stdout=data),
    ):
        with pytest.raises(expected):
            run_graphql(query_spec(), {})


def test_http_200_rate_limit_query_is_retryable() -> None:
    """A well-formed GraphQL rate-limit error is retry-safe for queries."""
    result = completed(
        stdout=json.dumps(
            {
                "data": None,
                "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
            }
        )
    )
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        return_value=result,
    ):
        with pytest.raises(GraphQLRetryableError):
            run_graphql(query_spec(), {})


@pytest.mark.parametrize("data", [None, {}, {"apparentlyComplete": True}])
def test_http_200_mutation_rate_limit_is_outcome_unknown(data: object) -> None:
    """Mutation rate-limit evidence never authorizes replay."""
    result = completed(
        stdout=json.dumps(
            {
                "data": data,
                "errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
            }
        )
    )
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        return_value=result,
    ):
        with pytest.raises(GraphQLMutationOutcomeUnknownError):
            run_graphql(update_review_comment_mutation("COMMENT", "new body"))


def test_unknown_mutation_transport_failure_carries_intent() -> None:
    """A status-less failure after dispatch is terminal and identifies its intent."""
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        side_effect=subprocess.TimeoutExpired(["gh"], 1),
    ):
        with pytest.raises(GraphQLMutationOutcomeUnknownError) as error:
            run_graphql(update_review_comment_mutation("COMMENT", "new body"))
    assert error.value.intent.operation == "updatePullRequestReviewComment"


def test_open_circuit_failure_is_the_only_safe_pre_dispatch_retry() -> None:
    """The circuit-breaker rejection is explicitly marked before dispatch."""
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        side_effect=GitHubUnavailableError("open"),
    ):
        with pytest.raises(GraphQLRetryableError) as error:
            run_graphql(query_spec(), {})
    assert error.value.pre_dispatch is True


def test_file_not_found_is_deterministic_for_mutations() -> None:
    """A missing executable proves no mutation request was launched."""
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        side_effect=FileNotFoundError("gh"),
    ):
        with pytest.raises(GraphQLDeterministicError):
            run_graphql(update_review_comment_mutation("COMMENT", "new body"))


def test_body_not_editable_is_the_special_mutation_rejection() -> None:
    """Only the exact normalized edit rejection enables shadow-comment recovery."""
    result = completed(
        returncode=1,
        stderr="Body is not editable",
    )
    with patch(
        "hephaestus.automation.github_api.graphql._raw_gh_call",
        return_value=result,
    ):
        with pytest.raises(ReviewCommentNotEditableError):
            run_graphql(update_review_comment_mutation("COMMENT", "new body"))
