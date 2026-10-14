"""Return typed rate-limit errors without extra automation requests."""

import json
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation.github_api.graphql import (
    GraphQLMutationOutcomeUnknownError,
    GraphQLQuerySpec,
    GraphQLRetryableError,
    update_review_comment_mutation,
)
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.github import rate_limit

_LIMIT_MESSAGE = "API rate limit exceeded"
_RESET_EPOCH = 1_700_000_000


@pytest.mark.parametrize("operation", ["query", "mutation"])
@pytest.mark.parametrize("response", ["status", "envelope", "exception"])
@pytest.mark.parametrize("stop", ["active", "expired", "cancelled"])
@pytest.mark.parametrize("cached", [False, True])
def test_rate_limit_classification_performs_no_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    operation: str,
    response: str,
    stop: str,
    cached: bool,
) -> None:
    """Keep reads retryable and mutation outcomes unknown after one dispatch."""
    now = [100.0]
    shutdown = threading.Event()
    monkeypatch.setattr("time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        rate_limit,
        "_rate_limit_probe_cache",
        {"graphql": (_RESET_EPOCH, 99.0)} if cached else {},
    )
    probe = Mock(
        return_value=subprocess.CompletedProcess(
            ["gh", "api", "rate_limit"],
            0,
            json.dumps({"resources": {"graphql": {"reset": _RESET_EPOCH}}}),
            "",
        )
    )
    monkeypatch.setattr(rate_limit, "run_subprocess", probe)

    def transport(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is False
        assert kwargs["max_retries"] == 1
        assert kwargs["retry_on_rate_limit"] is False
        assert kwargs["deadline_s"] == 110.0
        assert kwargs["shutdown"] is shutdown
        assert kwargs["timeout"] == 10.0
        if stop == "expired":
            now[0] = 111.0
        if stop == "cancelled":
            shutdown.set()
        if response == "exception":
            raise subprocess.CalledProcessError(1, argv, stderr=_LIMIT_MESSAGE)
        if response == "status":
            return subprocess.CompletedProcess(argv, 1, "", _LIMIT_MESSAGE)
        envelope = {
            "data": None,
            "errors": [{"type": "RATE_LIMITED", "message": _LIMIT_MESSAGE}],
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(envelope), "")

    call = Mock(side_effect=transport)
    github = PipelineGitHub("org", repo="repo", repo_root=tmp_path, command_runner=call)
    with github.operation_deadline(110.0, shutdown=shutdown):
        if operation == "query":
            spec = GraphQLQuerySpec(
                "reviewQuery", "query ReviewQuery { viewer { login } }", lambda data: data
            )
            with pytest.raises(GraphQLRetryableError) as caught:
                github._graphql(spec)
            assert caught.value.reset_epoch == (_RESET_EPOCH if cached else 0)
        else:
            with pytest.raises(GraphQLMutationOutcomeUnknownError) as unknown:
                github._graphql(update_review_comment_mutation("COMMENT", "reply"))
            assert unknown.value.intent.operation == "updatePullRequestReviewComment"
            assert unknown.value.intent.client_mutation_id

    call.assert_called_once()
    probe.assert_not_called()
