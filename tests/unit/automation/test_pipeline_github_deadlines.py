"""Check one-attempt GitHub dispatch and worker quota receipts."""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from hephaestus.automation import pipeline_github_transport
from hephaestus.automation.pipeline.github_jobs import (
    GitHubJob,
    RateBudgetRead,
    ReadRateBudgetRequest,
)
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner


def test_queue_transport_uses_one_attempt_and_the_total_deadline(tmp_path: Path) -> None:
    """Send the queue budget through the external command boundary."""
    result = subprocess.CompletedProcess(["gh"], 1, "", "HTTP 503 Service Unavailable")
    command = Mock(return_value=result)
    github = PipelineGitHub("org", repo="repo", repo_root=tmp_path, command_runner=command)
    deadline = time.monotonic() + 5.0
    with github.operation_deadline(deadline):
        assert github._deadline_gh_call(["api", "user"], check=False) is result
    assert command.call_count == 1
    assert command.call_args.kwargs["max_retries"] == 1
    assert command.call_args.kwargs["retry_on_rate_limit"] is False
    assert command.call_args.kwargs["deadline_s"] == deadline
    assert 0 < command.call_args.kwargs["timeout"] <= 5.0


@pytest.mark.parametrize("remaining", [0, 5000])
def test_quota_worker_returns_facts_without_a_recursive_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, remaining: int
) -> None:
    """Return quota data to coordinator timers through a closed receipt."""
    payload = {"resources": {"graphql": {"remaining": remaining, "reset": 1234}}}
    command = Mock(return_value=subprocess.CompletedProcess(["gh"], 0, json.dumps(payload), ""))
    monkeypatch.setattr(pipeline_github_transport, "gh_call", command)
    request = ReadRateBudgetRequest(deadline_s=time.monotonic() + 5.0)
    job = GitHubJob(repo="repo", repo_root=tmp_path, request=request, descr="Read quota")
    receipt = PipelineGitHubJobRunner("org", dry_run=False).run(job)
    assert receipt == RateBudgetRead(request=request, remaining=remaining, reset_epoch=1234)
    assert command.call_count == 1
    assert command.call_args.args == (["api", "rate_limit"],)
    assert command.call_args.kwargs["max_retries"] == 1
    assert command.call_args.kwargs["deadline_s"] == request.deadline_s
