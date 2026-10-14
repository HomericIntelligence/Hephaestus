"""Test active Check Runs through the reader, merge job, and wait stage."""

from __future__ import annotations

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.pipeline_github_required_checks as checks_mod
from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import StageOutcome
from hephaestus.automation.pipeline.stages.merge_wait import MergeWaitStage
from hephaestus.automation.pipeline_github import PipelineGitHub
from tests.unit.automation.pipeline.stages.test_stage_merge_wait import (
    _complete_merge_cycle,
    _ConditionalGitHub,
    _open_pr,
    _reviewed_item,
)

HEAD = "a" * 40


def _run(status: str = "in_progress", **changes: object) -> dict[str, object]:
    """Return one Check Run for the required context."""
    run: dict[str, object] = {
        "id": 1,
        "name": "required-ci",
        "head_sha": HEAD,
        "app": {"id": 1},
        "check_suite": {"id": 1},
        "status": status,
        "conclusion": "success" if status == "completed" else None,
        "completed_at": "2026-09-05T12:00:00Z" if status == "completed" else None,
    }
    run.update(changes)
    return run


@pytest.fixture
def reader_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Connect the real reader to the stage with controlled GitHub responses."""
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    github = _ConditionalGitHub()
    reads: list[list[dict[str, object]] | None] = [[_run()]]
    calls: list[str] = []

    def command(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        endpoint = argv[1]
        if "/check-suites?" in endpoint:
            payload: object = {
                "total_count": 1,
                "check_suites": [{"id": 1, "head_sha": HEAD, "app": {"id": 1}}],
            }
        elif "/check-runs?" in endpoint:
            calls.append("runs")
            runs = reads.pop(0) if len(reads) > 1 else reads[0]
            if runs is None:
                raise OSError("Check Runs are unavailable")
            payload = {"total_count": len(runs), "check_runs": runs}
        elif "/status?" in endpoint:
            payload = {"sha": HEAD, "total_count": 0, "statuses": []}
        else:
            raise AssertionError(f"Unexpected endpoint: {endpoint}")
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    adapter = PipelineGitHub("org", repo_root=tmp_path, command_runner=command)
    adapter.repo = "repo"
    monkeypatch.setattr(
        checks_mod, "_status_evidence_now_utc", lambda: datetime(2026, 9, 5, 12, tzinfo=UTC)
    )
    reader = MagicMock(wraps=adapter.required_checks_pass_for_head)
    monkeypatch.setattr(github, "required_checks_pass_for_head", reader)
    return github, reads, calls, reader


def test_stable_pending_checks_park_and_retry(
    reader_github: Any, make_ctx: Any, make_work_item: Any
) -> None:
    """Stable active evidence waits without a merge attempt."""
    github, _reads, calls, _reader = reader_github
    item = _reviewed_item(make_work_item)
    outcome = _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github))

    assert outcome == StageOutcome(Disposition.RETRY, "merge_readiness_wait")
    assert item.payload["retry_delay_s"] > 0
    assert item.attempts["merge"] == 0
    assert github.merge_attempts == []
    assert calls == ["runs", "runs"]


def test_pending_checks_reach_readiness_timeout(
    reader_github: Any, make_ctx: Any, make_work_item: Any
) -> None:
    """Repeated pending results keep the first readiness deadline."""
    github, _reads, calls, _reader = reader_github
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    now = [1000.0]
    ctx = make_ctx(github=github, now_fn=lambda: now[0])

    for _ in range(23):
        assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
            Disposition.RETRY, "merge_readiness_wait"
        )
        assert item.payload["merge_readiness_deadline_s"] == 2200.0
        now[0] += item.payload["retry_delay_s"]

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_FAIL, "merge_readiness_timeout"
    )
    assert github.merge_attempts == []
    assert item.attempts["merge"] == 0
    assert len(calls) == 46


def test_pending_checks_then_success_merge_same_reviewed_head(
    reader_github: Any, make_ctx: Any, make_work_item: Any
) -> None:
    """A later stable success permits a merge for the same reviewed head."""
    github, reads, calls, reader = reader_github
    item = _reviewed_item(make_work_item)
    stage = MergeWaitStage()
    ctx = make_ctx(github=github)
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    reads[:] = [[_run("completed")]]
    github._states = [_open_pr(), _open_pr(), {"state": "MERGED"}]

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FINISH_PASS, "merged"
    )
    assert github.merge_attempts == [(12, HEAD)]
    assert [call.args[0] for call in reader.call_args_list] == [HEAD, HEAD]
    assert len(calls) == 4


def test_queued_to_in_progress_requires_another_bounded_read(
    reader_github: Any, make_ctx: Any, make_work_item: Any
) -> None:
    """A lifecycle change needs a new scheduled pair of reads."""
    github, reads, calls, reader = reader_github
    reads[:] = [[_run("QUEUED")], [_run("IN_PROGRESS")]]
    item = _reviewed_item(make_work_item)
    ctx = make_ctx(github=github)
    stage = MergeWaitStage()

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert len(calls) == 2
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    assert reader.call_count == 2
    assert len(calls) == 4
    assert github.merge_attempts == []
    assert item.attempts["merge"] == 0


@pytest.mark.parametrize(
    "evidence",
    [
        [_run("completed", conclusion="failure")],
        [_run("completed", completed_at=None)],
        [],
        None,
        [_run(head_sha="b" * 40)],
        [_run(), _run("completed")],
        [_run(status="unknown")],
    ],
    ids=["failed", "malformed", "missing", "unavailable", "wrong-head", "duplicate", "lifecycle"],
)
def test_rejected_check_evidence_never_merges(
    reader_github: Any, make_ctx: Any, make_work_item: Any, evidence: Any
) -> None:
    """Invalid or unavailable required evidence cannot permit a merge."""
    github, reads, _calls, _reader = reader_github
    reads[:] = [evidence]
    item = _reviewed_item(make_work_item)

    assert _complete_merge_cycle(MergeWaitStage(), item, make_ctx(github=github)) == StageOutcome(
        Disposition.BLOCKED, "required_checks_not_green"
    )
    assert github.merge_attempts == []
    assert item.attempts["merge"] == 0


def test_pending_checks_cannot_reuse_proof_after_head_drift(
    reader_github: Any, make_ctx: Any, make_work_item: Any
) -> None:
    """A changed live head needs a new review before admission."""
    github, reads, calls, _reader = reader_github
    item = _reviewed_item(make_work_item)
    ctx = make_ctx(github=github)
    stage = MergeWaitStage()
    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.RETRY, "merge_readiness_wait"
    )
    reads[:] = [[_run("completed")]]
    github._states = [_open_pr(head="b" * 40)]

    assert _complete_merge_cycle(stage, item, ctx) == StageOutcome(
        Disposition.FAIL_BACK, "reviewed_head_drift"
    )
    assert github.merge_attempts == []
    assert len(calls) == 2
