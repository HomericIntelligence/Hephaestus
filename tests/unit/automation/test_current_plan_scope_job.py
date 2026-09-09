"""Read current plan scope through the bounded GitHub worker contract."""

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.pipeline import github_jobs
from hephaestus.automation.pipeline_github import PipelineGitHub
from hephaestus.automation.pipeline_github_jobs import PipelineGitHubJobRunner
from hephaestus.automation.review_journal import PlanDiscoveryResult

PLAN = "## Files to Modify\n- `src/b.py`\n- `src/a.py`\n"


def _request(repository: str = "org/repo") -> Any:
    """Build the required closed request without a fallback execution path."""
    request_type = getattr(github_jobs, "ReadCurrentPlanScopeRequest", None)
    assert request_type is not None, "Current plan reads need a closed worker request"
    return request_type(repository=repository, issue_number=7, deadline_s=time.monotonic() + 5)


def test_scope_read_uses_one_deadline_and_returns_exact_plan_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receipt keeps the exact plan digest and its canonical file declarations."""
    request = _request()
    shutdown = threading.Event()
    calls: list[int] = []

    def read(github: PipelineGitHub, issue: int) -> PlanDiscoveryResult:
        assert github._operation_deadline_s == request.deadline_s
        assert github._operation_shutdown is shutdown
        calls.append(issue)
        return PlanDiscoveryResult.found(PLAN)

    monkeypatch.setattr(PipelineGitHub, "discover_plan", read)
    job = github_jobs.GitHubJob("repo", tmp_path, request, "Read current plan scope")

    receipt = PipelineGitHubJobRunner("org", dry_run=False).run(job, shutdown=shutdown)

    assert isinstance(receipt, github_jobs.CurrentPlanScopeRead)
    assert receipt.request == request
    assert receipt.paths == ("src/a.py", "src/b.py")
    assert receipt.plan_sha256 == hashlib.sha256(PLAN.encode("utf-8")).hexdigest()
    assert calls == [7]


@pytest.mark.parametrize(
    "discovery",
    [
        PlanDiscoveryResult.absent(),
        PlanDiscoveryResult.read_error("transport failed"),
        PlanDiscoveryResult.identity_conflict("foreign pointer"),
        PlanDiscoveryResult.found("Plan without declared files"),
    ],
)
def test_scope_read_never_returns_authority_from_missing_or_invalid_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, discovery: PlanDiscoveryResult
) -> None:
    """Unavailable or ambiguous scope fails before a writer request can be built."""
    request = _request()
    monkeypatch.setattr(PipelineGitHub, "discover_plan", lambda *_args: discovery)
    job = github_jobs.GitHubJob("repo", tmp_path, request, "Read current plan scope")

    with pytest.raises((RuntimeError, ValueError)):
        PipelineGitHubJobRunner("org", dry_run=False).run(job)


def test_scope_read_rejects_another_repository_before_service_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal issue numbers cannot cross the repository boundary."""
    request = _request("other/repo")

    def forbidden(*_args: object) -> PlanDiscoveryResult:
        pytest.fail("repository mismatch reached GitHub")

    monkeypatch.setattr(PipelineGitHub, "discover_plan", forbidden)
    job = github_jobs.GitHubJob("repo", tmp_path, request, "Read current plan scope")
    with pytest.raises(ValueError, match="repository"):
        PipelineGitHubJobRunner("org", dry_run=False).run(job)
