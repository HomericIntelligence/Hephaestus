"""Regression coverage for lease-backed direct issue source reconciliation."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.issue_waves import (
    WAVE_NON_CODE_INTENT_PAYLOAD,
    IssueWaveStore,
)
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.requirements_recovery import evidence_digest
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


def test_lease_backed_non_code_intent_reenters_planning_for_label_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash before state:skip is repaired from the durable reviewed intent."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2453])
    reason = "independently confirmed tracker"
    store.record_non_code_intent(
        lease,
        issue_number=2453,
        reason=reason,
        evidence_digest=evidence_digest("repo-a", 2453, BASE_SHA, "A task", ""),
        repository_revision=BASE_SHA,
        extra_labels=("epic",),
    )
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
        github=FakeStageGitHub(labels=["state:needs-plan"]),
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
    item = coordinator.queues[StageName.PLANNING].snapshot()[0]
    assert item.payload[WAVE_NON_CODE_INTENT_PAYLOAD] == {
        "reason": reason,
        "extra_labels": ["epic"],
        "evidence_digest": evidence_digest("repo-a", 2453, BASE_SHA, "A task", ""),
        "repository_revision": BASE_SHA,
        "explanation": "",
        "retired": False,
    }


def test_lease_backed_retired_intent_projects_cleanup_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A process restart carries revoked intent evidence into planning cleanup."""
    repo_root = tmp_path / "repo-a"
    repo_root.mkdir()
    store = IssueWaveStore(repo_root, "org", "repo-a")
    lease = store.seal_selection(store.plan_admission(BASE_SHA, 1), [2454])
    store.record_non_code_intent(
        lease,
        issue_number=2454,
        reason="independently confirmed tracker",
        evidence_digest=evidence_digest("repo-a", 2454, BASE_SHA, "A task", "Old body"),
        repository_revision=BASE_SHA,
        extra_labels=("epic",),
    )
    active = store.non_code_intent_for(lease, 2454)
    assert active is not None
    store.retire_non_code_intent(lease, active)
    config = PipelineConfig(
        org="org",
        repos=["repo-a"],
        issues=[2454],
        loops=1,
        projects_dir=tmp_path,
        repo_roots={"repo-a": repo_root},
    )
    coordinator = Coordinator(
        config,
        github=FakeStageGitHub(
            labels=["state:skip", "epic"],
            issue_body="Implement the worker.",
        ),
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
    item = coordinator.queues[StageName.PLANNING].snapshot()[0]
    assert item.payload[WAVE_NON_CODE_INTENT_PAYLOAD]["retired"] is True
