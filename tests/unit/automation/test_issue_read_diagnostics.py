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


@pytest.mark.parametrize("scoped", [False, True], ids=["legacy", "scoped"])
@pytest.mark.parametrize(
    ("failure", "category"),
    [
        (
            subprocess.CalledProcessError(
                1, "secret-command", stderr="dial tcp secret-host: i/o timeout"
            ),
            "tcp-timeout",
        ),
        (
            subprocess.CalledProcessError(
                1, "secret-command", stderr=b"net/http: TLS handshake timeout secret-token"
            ),
            "tls-timeout",
        ),
        (
            subprocess.TimeoutExpired("secret-command", 30, stderr="secret-token"),
            "subprocess-timeout",
        ),
        (
            subprocess.CalledProcessError(1, "secret-command", stderr="secret-token"),
            "subprocess-failed",
        ),
        (OSError("secret-path"), "os-error"),
    ],
)
def test_issue_failure_category_reaches_terminal_summary(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scoped: bool,
    failure: Exception,
    category: str,
) -> None:
    """Both issue readers preserve the cause and emit only a fixed category."""
    adapter = PipelineGitHub("org", repo="repo" if scoped else None)
    if scoped:
        monkeypatch.setattr(adapter, "_gh", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(github_api, "_gh_call", Mock(side_effect=failure))

    with pytest.raises(RuntimeError) as caught:
        adapter.gh_issue_json(3112)

    assert str(caught.value) == f"Failed to fetch issue #3112: {category}"
    assert caught.value.__cause__ is failure
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=3112, stage=StageName.FINISHED)
    item.result = ItemResult(
        passed=False, reason=f"poisoned: {caught.value}", final_stage=StageName.FINISHED
    )
    with caplog.at_level(logging.INFO):
        print_summary([item], RunStats(1, 1, 0, 0.0, 1.0), [], json_out=False)
    assert category in caplog.text
    assert "secret-" not in caplog.text
