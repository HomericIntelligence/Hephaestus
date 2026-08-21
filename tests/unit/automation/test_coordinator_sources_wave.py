"""Regression coverage for lease-backed direct issue source reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.issue_waves import IssueWaveStore
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub

BASE_SHA = "a" * 40


@pytest.mark.parametrize("label", ["state:skip", "state:plan-blocked"])
def test_lease_backed_direct_exclusion_reaches_finished_and_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    label: str,
) -> None:
    """Recovery waves checkpoint terminal overrides instead of dropping them."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2453])
    github = FakeStageGitHub(labels=[label])
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[2453],
        loops=1,
        projects_dir=tmp_path,
        repo_roots={"repo-a": repo_root},
    )
    coordinator = Coordinator(
        config,
        github=github,
        pool=FakeWorkerPool(),
        install_signals=False,
    )
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.coordinator._admission._filter_open_issues",
        lambda _repo, issues: list(issues),
    )
    coordinator._direct_wave_lease = lease
    coordinator._begin_direct_issue_source("repo-a", BASE_SHA)

    assert coordinator._drain_direct_issue_source() == 1
    item = coordinator.queues[StageName.FINISHED].snapshot()[0]
    assert item.result is not None
    assert item.result.passed is False
    assert "became skipped or blocked" in item.result.reason
    assert github.mutation_log == []

    coordinator._drain_queues()

    checkpoint = store.load()
    assert checkpoint is not None
    outcome = checkpoint.current_wave.outcomes[0]
    assert outcome.issue_number == 2453
    assert outcome.passed is False
    assert outcome.reason == item.result.reason
