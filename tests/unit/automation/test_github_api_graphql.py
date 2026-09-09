"""Contract tests for the automation GraphQL execution boundary."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import Mock, patch

import pytest

from hephaestus.automation.github_api.graphql import (
    GraphQLDeterministicError,
    GraphQLMutationIntent,
    GraphQLMutationOutcomeUnknownError,
    GraphQLQuerySpec,
    GraphQLResponseError,
    GraphQLRetryableError,
    MergeQueueAlreadyEnqueuedError,
    ReviewCommentNotEditableError,
    add_implementation_thread_reply_mutation,
    create_pending_review_mutation,
    enqueue_pull_request_mutation,
    pipeline_thread_snapshot_page_query,
    pull_request_queue_entry_query,
    resolve_thread_mutation,
    run_graphql,
    submit_review_mutation,
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


@pytest.mark.parametrize("returncode", [0, 1], ids=("graphql-envelope", "nonzero-envelope"))
def test_enqueue_already_queued_has_a_dedicated_typed_error(returncode: int) -> None:
    """The sole exact rejection has a type that permits bounded reconciliation."""
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
            return_value=completed(stdout=json.dumps(response), returncode=returncode),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(GraphQLMutationOutcomeUnknownError) as error,
    ):
        run_graphql(spec)

    assert type(error.value) is MergeQueueAlreadyEnqueuedError
    assert str(error.value) == "Pull request is already in the queue"
    assert error.value.intent.operation == "enqueuePullRequest"


@pytest.mark.parametrize(
    ("spec_kind", "errors"),
    [
        (
            "other-operation",
            [{"type": "UNPROCESSABLE", "message": "Pull request is already in the queue"}],
        ),
        (
            "enqueue",
            [{"type": "FORBIDDEN", "message": "Pull request is already in the queue"}],
        ),
        (
            "enqueue",
            [{"type": "UNPROCESSABLE", "message": "Pull request is already queued"}],
        ),
        (
            "enqueue",
            [
                {"type": "UNPROCESSABLE", "message": "Pull request is already in the queue"},
                {"type": "FORBIDDEN", "message": "another error"},
            ],
        ),
    ],
    ids=("other-operation", "other-type", "other-message", "multiple-errors"),
)
@pytest.mark.parametrize("returncode", [0, 1], ids=("graphql-envelope", "nonzero-envelope"))
def test_enqueue_already_queued_lookalikes_remain_outcome_unknown(
    spec_kind: str,
    errors: list[dict[str, str]],
    returncode: int,
) -> None:
    """Text alone cannot grant permission for a queue-entry readback."""
    spec = (
        enqueue_pull_request_mutation("PR_node", "a" * 40)
        if spec_kind == "enqueue"
        else update_review_comment_mutation("COMMENT", "body")
    )
    response = {"data": {spec.operation: None}, "errors": errors}
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(stdout=json.dumps(response), returncode=returncode),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(GraphQLMutationOutcomeUnknownError) as error,
    ):
        run_graphql(spec)

    assert type(error.value) is GraphQLMutationOutcomeUnknownError


def test_enqueue_already_queued_process_prose_remains_outcome_unknown() -> None:
    """Unstructured process output cannot prove the exact GitHub error type."""
    spec = enqueue_pull_request_mutation("PR_node", "a" * 40)
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(
                stderr="Pull request is already in the queue",
                returncode=1,
            ),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(GraphQLMutationOutcomeUnknownError) as error,
    ):
        run_graphql(spec)

    assert type(error.value) is GraphQLMutationOutcomeUnknownError


def test_enqueue_already_queued_transport_error_is_an_outcome_unknown_error() -> None:
    """The transport classifier preserves GitHub's exact queue error text."""
    spec = enqueue_pull_request_mutation("PR_node", "a" * 40)
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(
                stdout="",
                returncode=1,
                stderr="UNPROCESSABLE: Pull request is already in the queue",
            ),
        ),
        patch(
            "hephaestus.automation.github_api.graphql.uuid.uuid4",
            return_value=Mock(hex="queue-id"),
        ),
        pytest.raises(GraphQLMutationOutcomeUnknownError) as raised,
    ):
        run_graphql(spec)
    assert str(raised.value).strip() == "UNPROCESSABLE: Pull request is already in the queue"


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


@pytest.mark.parametrize(
    "pull_request",
    [
        None,
        {
            "id": "PR_node",
            "number": 7,
            "state": "CLOSED",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "AWAITING_CHECKS"},
        },
        {
            "id": "PR_node",
            "number": 7,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": None,
        },
        {
            "id": "PR_node",
            "number": 7,
            "state": "OPEN",
            "headRefOid": "a" * 40,
            "mergeQueueEntry": {"id": "MQE_node", "state": "UNKNOWN"},
        },
    ],
    ids=("missing-pr", "closed-pr", "missing-entry", "invalid-entry-state"),
)
def test_pull_request_queue_entry_query_rejects_invalid_readback(
    pull_request: object,
) -> None:
    """A readback is valid only for one open pull request with a queue entry."""
    spec = pull_request_queue_entry_query("org", "repo", 7)
    response = {
        "data": {
            "repository": {
                "owner": {"login": "org"},
                "name": "repo",
                "pullRequest": pull_request,
            }
        }
    }
    with (
        patch(
            "hephaestus.automation.github_api.graphql._raw_gh_call",
            return_value=completed(stdout=json.dumps(response)),
        ),
        pytest.raises(GraphQLDeterministicError),
    ):
        run_graphql(spec, {"owner": "org", "name": "repo", "number": 7})


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


def _thread_snapshot_data() -> dict[str, Any]:
    """Return a thread page with an owned reply and an external comment."""
    repository = {"name": "repo", "owner": {"login": "org"}}
    return {
        "repository": {
            **repository,
            "pullRequest": {
                "id": "PR_node",
                "number": 7,
                "state": "OPEN",
                "headRefOid": "a" * 40,
                "autoMergeRequest": None,
            },
        },
        "node": {
            "id": "THREAD",
            "isResolved": False,
            "path": "src/app.py",
            "line": 12,
            "side": "RIGHT",
            "pullRequest": {"id": "PR_node", "number": 7, "repository": repository},
            "comments": {
                "pageInfo": {"hasNextPage": True, "endCursor": "next-page"},
                "nodes": [
                    {
                        "id": "REPLY",
                        "body": "Change complete.",
                        "viewerDidAuthor": True,
                        "author": {"login": "worker", "__typename": "User"},
                        "pullRequestReview": {
                            "id": "REVIEW",
                            "state": "PENDING",
                            "body": "Implementation replies.",
                            "commit": {"oid": "a" * 40},
                        },
                    },
                    {
                        "id": "FINDING",
                        "body": "Check this change.",
                        "viewerDidAuthor": False,
                        "author": None,
                        "pullRequestReview": None,
                    },
                ],
            },
        },
    }


def _assert_one_attempt(call: Mock) -> None:
    """Require one transport attempt with transport retries disabled."""
    call.assert_called_once()
    assert call.call_args.kwargs["max_retries"] == 1
    assert call.call_args.kwargs["retry_on_rate_limit"] is False
    assert call.call_args.kwargs["throttle"] is False


@pytest.mark.parametrize(
    ("state", "auto_merge"),
    [("OPEN", None), ("CLOSED", {"enabledAt": "2026-09-09T00:00:00Z"})],
    ids=("open-unarmed", "closed-armed"),
)
def test_thread_snapshot_preserves_identity_state_ownership_and_pagination(
    state: str, auto_merge: dict[str, str] | None
) -> None:
    """The query returns live facts for the caller's publication decision."""
    data = _thread_snapshot_data()
    data["repository"]["pullRequest"].update(state=state, autoMergeRequest=auto_merge)
    call = Mock(return_value=completed(stdout=json.dumps({"data": data})))

    result = run_graphql(
        pipeline_thread_snapshot_page_query("org", "repo", 7, "THREAD"),
        {"owner": "org", "name": "repo", "number": 7, "threadId": "THREAD", "after": "prior"},
        call=call,
    )

    assert result["pr_node_id"] == "PR_node"
    assert result["pr_state"] == {
        "state": state,
        "headRefOid": "a" * 40,
        "autoMergeRequest": auto_merge,
    }
    assert result["thread"]["id"] == "THREAD"
    assert result["thread"]["isResolved"] is False
    assert result["comments"]["pageInfo"] == {"hasNextPage": True, "endCursor": "next-page"}
    owned, external = result["comments"]["nodes"]
    assert owned["viewerDidAuthor"] is True
    assert owned["author"]["login"] == "worker"
    assert owned["pullRequestReview"]["id"] == "REVIEW"
    assert owned["pullRequestReview"]["commit"]["oid"] == "a" * 40
    assert external["viewerDidAuthor"] is False
    assert external["author"] is None
    assert external["pullRequestReview"] is None
    assert "after=prior" in call.call_args.args[0]
    assert "threadId=THREAD" in call.call_args.args[0]
    _assert_one_attempt(call)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("repository", "owner", "login"), "another-owner"),
        (("repository", "pullRequest"), None),
        (("repository", "pullRequest", "number"), 8),
        (("repository", "pullRequest", "headRefOid"), None),
        (("repository", "pullRequest", "autoMergeRequest"), False),
        (("node", "id"), "OTHER_THREAD"),
        (("node", "pullRequest", "id"), "OTHER_PR"),
        (("node", "isResolved"), "false"),
        (("node", "comments", "pageInfo", "hasNextPage"), None),
        (("node", "comments", "nodes", 0, "viewerDidAuthor"), "true"),
        (("node", "comments", "nodes", 0, "author"), {"login": None}),
        (("node", "comments", "nodes", 0, "pullRequestReview"), "REVIEW"),
        (("node", "comments", "nodes", 0, "pullRequestReview", "commit"), None),
    ],
    ids=(
        "wrong-repository",
        "missing-pr",
        "wrong-pr-number",
        "missing-head",
        "invalid-auto-merge-state",
        "wrong-thread",
        "thread-on-another-pr",
        "invalid-resolution-state",
        "unknown-page-completeness",
        "invalid-comment-ownership",
        "invalid-author",
        "invalid-review",
        "missing-review-commit",
    ),
)
def test_thread_snapshot_rejects_incomplete_or_mismatched_evidence(
    path: tuple[str | int, ...], value: object
) -> None:
    """Invalid readback evidence raises a read error after one attempt."""
    data = _thread_snapshot_data()
    parent: Any = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    call = Mock(return_value=completed(stdout=json.dumps({"data": data})))

    with pytest.raises(GraphQLDeterministicError):
        run_graphql(
            pipeline_thread_snapshot_page_query("org", "repo", 7, "THREAD"),
            {"owner": "org", "name": "repo", "number": 7, "threadId": "THREAD"},
            call=call,
        )

    _assert_one_attempt(call)


@pytest.fixture
def receipt_call(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Use a fixed request ID with an explicit transport seam."""
    monkeypatch.setattr(
        "hephaestus.automation.github_api.graphql.uuid.uuid4", lambda: Mock(hex="receipt-id")
    )
    return Mock()


def _set_receipt(call: Mock, operation: str, field: str, receipt: object) -> None:
    """Return a correlated mutation receipt through the transport seam."""
    call.return_value = completed(
        stdout=json.dumps({"data": {operation: {"clientMutationId": "receipt-id", field: receipt}}})
    )


def _review_receipt(state: str) -> dict[str, Any]:
    """Return the review identity for the requested PR and head."""
    return {
        "id": "REVIEW",
        "state": state,
        "pullRequest": {"id": "PR_node"},
        "commit": {"oid": "a" * 40},
    }


@pytest.mark.parametrize("submit", [False, True], ids=("create", "submit"))
def test_review_write_receipt_matches_pr_head_and_expected_state(
    receipt_call: Mock, submit: bool
) -> None:
    """Create and submit preserve the exact PR, head, and review state."""
    operation = "submitPullRequestReview" if submit else "addPullRequestReview"
    review = _review_receipt("COMMENTED" if submit else "PENDING")
    _set_receipt(receipt_call, operation, "pullRequestReview", review)
    spec = (
        submit_review_mutation("REVIEW", "PR_node", "a" * 40)
        if submit
        else create_pending_review_mutation("PR_node", "a" * 40, "Implementation replies.")
    )

    result = run_graphql(spec, call=receipt_call)

    assert result == {"clientMutationId": "receipt-id", **review}
    argv = receipt_call.call_args.args[0]
    assert "clientMutationId=receipt-id" in argv
    if submit:
        assert "reviewId=REVIEW" in argv
    else:
        assert "pullRequestId=PR_node" in argv
        assert "headSha=" + "a" * 40 in argv
        assert "body=Implementation replies." in argv
    _assert_one_attempt(receipt_call)


@pytest.mark.parametrize("submit", [False, True], ids=("create", "submit"))
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", None),
        ("state", "APPROVED"),
        ("pullRequest", {"id": "OTHER_PR"}),
        ("commit", {"oid": "b" * 40}),
    ],
    ids=("invalid-review-id", "wrong-state", "another-pr", "stale-head"),
)
def test_review_write_mismatched_receipt_keeps_unknown_outcome(
    receipt_call: Mock, submit: bool, field: str, value: object
) -> None:
    """A mismatched review receipt cannot permit another mutation attempt."""
    operation = "submitPullRequestReview" if submit else "addPullRequestReview"
    review = _review_receipt("COMMENTED" if submit else "PENDING")
    review[field] = "OTHER_REVIEW" if submit and field == "id" else value
    _set_receipt(receipt_call, operation, "pullRequestReview", review)
    spec = (
        submit_review_mutation("REVIEW", "PR_node", "a" * 40)
        if submit
        else create_pending_review_mutation("PR_node", "a" * 40, "Implementation replies.")
    )

    with pytest.raises(GraphQLMutationOutcomeUnknownError) as error:
        run_graphql(spec, call=receipt_call)

    assert error.value.intent.operation == operation
    assert error.value.intent.targets == (
        (("reviewId", "REVIEW"),) if submit else (("pullRequestId", "PR_node"),)
    )
    assert error.value.intent.client_mutation_id == "receipt-id"
    _assert_one_attempt(receipt_call)


def _reply_receipt() -> dict[str, Any]:
    """Return an owned reply in the expected pending review."""
    return {
        "id": "REPLY",
        "body": "Change complete.",
        "viewerDidAuthor": True,
        "pullRequestReview": {
            "id": "REVIEW",
            "state": "PENDING",
            "commit": {"oid": "a" * 40},
        },
    }


def test_reply_receipt_proves_body_ownership_and_pending_review(receipt_call: Mock) -> None:
    """An owned reply receipt identifies the pending review for later readback."""
    reply = _reply_receipt()
    _set_receipt(receipt_call, "addPullRequestReviewThreadReply", "comment", reply)

    result = run_graphql(
        add_implementation_thread_reply_mutation(
            "THREAD", "Change complete.", pending_review_id="REVIEW", expected_head_sha="a" * 40
        ),
        call=receipt_call,
    )

    assert result == {"clientMutationId": "receipt-id", **reply}
    assert "threadId=THREAD" in receipt_call.call_args.args[0]
    assert "body=Change complete." in receipt_call.call_args.args[0]
    _assert_one_attempt(receipt_call)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("body", "Another reply."),
        ("viewerDidAuthor", False),
        ("pullRequestReview", None),
        ("pullRequestReview", {"id": "OTHER_REVIEW", "state": "PENDING"}),
        ("pullRequestReview", {"id": "REVIEW", "state": "COMMENTED"}),
    ],
    ids=("changed-body", "not-owned", "missing-review", "another-review", "already-submitted"),
)
def test_reply_receipt_mismatch_retains_intent_without_replay(
    receipt_call: Mock, field: str, value: object
) -> None:
    """An incomplete reply receipt requires reconciliation of the saved intent."""
    reply = _reply_receipt()
    reply[field] = value
    _set_receipt(receipt_call, "addPullRequestReviewThreadReply", "comment", reply)

    with pytest.raises(GraphQLMutationOutcomeUnknownError) as error:
        run_graphql(
            add_implementation_thread_reply_mutation(
                "THREAD", "Change complete.", pending_review_id="REVIEW", expected_head_sha="a" * 40
            ),
            call=receipt_call,
        )

    assert error.value.intent.operation == "addPullRequestReviewThreadReply"
    assert error.value.intent.targets == (("threadId", "THREAD"),)
    assert error.value.intent.content_hashes
    assert "Change complete." not in error.value.intent.safe_summary()
    _assert_one_attempt(receipt_call)


def test_resolve_receipt_identifies_the_resolved_thread(receipt_call: Mock) -> None:
    """Resolution succeeds only with a receipt for the requested thread."""
    _set_receipt(
        receipt_call, "resolveReviewThread", "thread", {"id": "THREAD", "isResolved": True}
    )

    assert run_graphql(resolve_thread_mutation("THREAD"), call=receipt_call) == {
        "clientMutationId": "receipt-id",
        "id": "THREAD",
        "isResolved": True,
    }
    assert "threadId=THREAD" in receipt_call.call_args.args[0]
    _assert_one_attempt(receipt_call)


@pytest.mark.parametrize(
    "thread",
    [None, {"id": "OTHER_THREAD", "isResolved": True}, {"id": "THREAD", "isResolved": False}],
    ids=("missing-thread", "another-thread", "still-unresolved"),
)
def test_resolve_receipt_mismatch_is_unknown_without_replay(
    receipt_call: Mock, thread: object
) -> None:
    """An invalid resolution receipt preserves the uncertain thread write."""
    _set_receipt(receipt_call, "resolveReviewThread", "thread", thread)

    with pytest.raises(GraphQLMutationOutcomeUnknownError) as error:
        run_graphql(resolve_thread_mutation("THREAD"), call=receipt_call)

    assert error.value.intent.operation == "resolveReviewThread"
    assert error.value.intent.targets == (("threadId", "THREAD"),)
    _assert_one_attempt(receipt_call)
