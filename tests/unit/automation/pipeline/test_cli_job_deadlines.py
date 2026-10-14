"""Keep role limits when a command also sets an outer operation limit."""

from pathlib import Path

import pytest

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.jobs import AgentJob
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stage_results import JobRequest
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline_cli import build_config, parse_args
from tests.unit.automation.pipeline.conftest import FakeWorkerPool, fake_worker_factories
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


@pytest.mark.parametrize("profile", ["full", "planning", "implementation", "review"])
@pytest.mark.parametrize(
    ("outer", "expected_timeout", "expected_deadline"),
    [(7800.0, 60, 160.0), (20.0, 20, 120.0), (0.0, 60, None), (0.25, 1, 100.25)],
)
def test_cli_outer_bound_preserves_role_limit(
    tmp_path: Path,
    profile: str,
    outer: float,
    expected_timeout: int,
    expected_deadline: float | None,
) -> None:
    """The submitted job keeps the smaller positive bound without a zero timeout."""
    args = parse_args(
        [
            "--agent",
            "claude",
            "--projects-dir",
            str(tmp_path),
            "--planner-timeout",
            "60",
            "--phase-timeout",
            str(outer),
        ],
        profile=profile,
    )
    config = build_config(args, "org", ["repo"])
    pool = FakeWorkerPool()
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(),
        **fake_worker_factories(pool),
        install_signals=False,
        monotonic=lambda: 100.0,
    )
    item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=1, stage=StageName.PLANNING)
    job = AgentJob(
        repo=item.repo,
        issue=1,
        agent="claude",
        model="default",
        prompt_builder=lambda: "Plan the change.",
        cwd=tmp_path,
        timeout_s=config.planner_timeout,
    )

    coordinator._submit_ready_job(item, JobRequest(job, "DONE"))

    submitted = pool.submitted[0].job
    assert isinstance(submitted, AgentJob)
    assert submitted.timeout_s == expected_timeout
    assert submitted.deadline_s == expected_deadline
