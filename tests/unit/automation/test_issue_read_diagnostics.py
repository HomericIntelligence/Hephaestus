"""Issue reads retain failure categories without exposing subprocess data."""

import logging
import subprocess
from unittest.mock import Mock

import pytest

from hephaestus.automation import github_api
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.summary import RunStats, print_summary
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.git.gh.rate_limit import OperationDeadlineExpired


@pytest.mark.parametrize("scoped", [False, True], ids=["legacy", "scoped"])
@pytest.mark.parametrize(
    ("failure", "category", "cause"),
    [
        (
            subprocess.CalledProcessError(
                1, "secret-command", stderr="dial tcp secret-host: i/o timeout"
            ),
            "tcp-timeout",
            "gh exited with status 1",
        ),
        (
            subprocess.CalledProcessError(
                1, "secret-command", stderr=b"net/http: TLS handshake timeout secret-token"
            ),
            "tls-timeout",
            "gh exited with status 1",
        ),
        (
            subprocess.TimeoutExpired("secret-command", 30, stderr="secret-token"),
            "subprocess-timeout",
            "subprocess timed out after 30 seconds",
        ),
        (
            OperationDeadlineExpired("GitHub operation deadline"),
            "subprocess-timeout",
            "absolute monotonic deadline expired before the next GitHub request",
        ),
        (
            subprocess.CalledProcessError(1, "secret-command", stderr="secret-token"),
            "subprocess-failed",
            "gh exited with status 1",
        ),
        (PermissionError(13, "secret-path"), "os-error", "PermissionError (errno=13)"),
    ],
)
def test_issue_failure_category_reaches_terminal_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scoped: bool,
    failure: Exception,
    category: str,
    cause: str,
) -> None:
    """Issue readers report a safe cause without exposing subprocess data."""
    adapter = PipelineGitHub("org", repo="repo" if scoped else None)
    if scoped:
        monkeypatch.setattr(adapter, "_gh", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(github_api, "_gh_call", Mock(side_effect=failure))

    with pytest.raises(RuntimeError) as caught:
        adapter.gh_issue_json(3112)

    assert str(caught.value).startswith(f"Failed to fetch issue #3112: {category}; ")
    assert cause in str(caught.value)
    assert caught.value.__cause__ is failure
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=3112, stage=StageName.FINISHED)
    item.result = ItemResult(
        passed=False, reason=f"poisoned: {caught.value}", final_stage=StageName.FINISHED
    )
    with caplog.at_level(logging.INFO):
        print_summary([item], RunStats(1, 1, 0, 0.0, 1.0), [], json_out=False)
    assert category in caplog.text
    assert "secret-" not in caplog.text
