"""Unit tests for the implementer checkpoint module (issue #3435).

Every Git and clock seam is faked: ``implementer_checkpoint.run_git`` is
monkeypatched per test and worktrees live under ``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.agent_config import AGENT_IMPLEMENTER
from hephaestus.automation.pipeline import implementer_checkpoint
from hephaestus.automation.pipeline.implementer_checkpoint import (
    ImplementerCheckpoint,
    LaneSnapshot,
    apply_timeout_session_continuity,
    checkpoint_path,
    classify_agent_completion,
    collect_lane,
    discard_checkpoint,
    interval_from_config,
    lane_identity,
    observe_in_flight,
    read_checkpoint,
    resume_context_for,
    stamp_turn,
    validate_checkpoint,
    write_checkpoint,
)
from hephaestus.automation.pipeline.jobs import AgentJob, JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.constants import IMPLEMENTER_CHECKPOINT_INTERVAL_S

HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["git"], 0, stdout, "")


def _fake_run_git(
    *,
    git_dir: Path,
    head: str = HEAD,
    status: str = "",
) -> Any:
    """Return a run_git stand-in answering the checkpoint metadata queries."""

    def _run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        subcommand = args[2:]
        if subcommand == ["rev-parse", "--git-dir"]:
            return _completed(f"{git_dir}\n")
        if subcommand == ["rev-parse", "HEAD"]:
            return _completed(f"{head}\n")
        if subcommand == ["status", "--porcelain=v1"]:
            return _completed(status)
        raise AssertionError(f"unexpected git invocation: {args!r}")

    return _run


def _raising_run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise AssertionError(f"disabled path must not call git: {args!r}")


def _failing_run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    raise subprocess.CalledProcessError(1, args, "", "fatal: not a git repository")


def _config(**overrides: Any) -> Any:
    return SimpleNamespace(**overrides)


def _lane(tmp_path: Path, *, files: tuple[str, ...] = ("a.py",)) -> LaneSnapshot:
    git_dir = tmp_path / "lane" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)
    return LaneSnapshot(git_dir=git_dir, head_sha=HEAD, changed_files=files)


def _write_test_checkpoint(
    lane: LaneSnapshot, *, issue: int = 1, branch: str = "1-auto-impl", worktree: str, **kwargs: Any
) -> ImplementerCheckpoint:
    defaults = {"elapsed_s": 42.0, "attempt": 1, "step": "turn in progress"}
    defaults.update(kwargs)
    return write_checkpoint(lane, issue=issue, branch=branch, worktree=worktree, **defaults)


def _make_item(
    *,
    issue: int | None = 1,
    stage: StageName = StageName.IMPLEMENTATION,
    worktree: str = "",
    branch: str = "1-auto-impl",
    stamp: float | None = None,
) -> WorkItem:
    item = WorkItem(
        repo="test-repo",
        kind=ItemKind.ISSUE,
        issue=issue,
        pr=None,
        stage=stage,
        state="IMPLEMENT_WAIT",
    )
    item.branch = branch
    item.worktree = worktree
    if stamp is not None:
        stamp_turn(item, now=stamp)
    return item


def _make_handle(tmp_path: Path, *, session_agent: str = AGENT_IMPLEMENTER) -> JobHandle:
    job = AgentJob(
        repo="test-repo",
        issue=1,
        agent="claude",
        model="model",
        prompt_builder=lambda: "",
        cwd=tmp_path,
        timeout_s=60,
        session_agent=session_agent,
        descr="implement",
    )
    return JobHandle(job=job, on_done_state="TEST_WAIT")


class TestIntervalFromConfig:
    """Config-read behavior of the interval accessor."""

    def test_default_when_unset(self) -> None:
        assert interval_from_config(_config()) == IMPLEMENTER_CHECKPOINT_INTERVAL_S

    def test_zero_is_accepted(self) -> None:
        assert interval_from_config(_config(implementer_checkpoint_interval=0)) == 0

    def test_positive_value(self) -> None:
        assert interval_from_config(_config(implementer_checkpoint_interval=60)) == 60

    def test_negative_fails(self) -> None:
        with pytest.raises(ValueError, match="must be nonnegative"):
            interval_from_config(_config(implementer_checkpoint_interval=-1))


class TestCheckpointRoundTrip:
    """Write, read, validate, and discard one checkpoint record."""

    def test_write_read_validate(self, tmp_path: Path) -> None:
        lane = _lane(tmp_path)
        worktree = str(tmp_path / "lane")
        checkpoint = _write_test_checkpoint(lane, worktree=worktree, last_test_result="17 passed")
        path = checkpoint_path(lane.git_dir)
        assert path.exists()

        raw = read_checkpoint(path)
        assert raw is not None
        restored = validate_checkpoint(raw, issue=1, branch="1-auto-impl", worktree=worktree)
        assert restored is not None
        assert restored == checkpoint
        assert restored.elapsed_s == 42.0
        assert restored.changed_files == ("a.py",)
        assert restored.last_test_result == "17 passed"

    def test_read_checkpoint_missing_or_undecodable(self, tmp_path: Path) -> None:
        missing = tmp_path / "lane" / ".git" / "checkpoint.json"
        assert read_checkpoint(missing) is None
        missing.parent.mkdir(parents=True)
        missing.write_text("not json", encoding="utf-8")
        assert read_checkpoint(missing) is None
        missing.write_text(json.dumps(["a", "list"]), encoding="utf-8")
        assert read_checkpoint(missing) is None

    def test_write_bounds_long_fields(self, tmp_path: Path) -> None:
        files = (*tuple(f"src/file{i}.py" for i in range(60)), "x" * 500)
        lane = _lane(tmp_path, files=files)
        checkpoint = _write_test_checkpoint(
            lane,
            worktree=str(tmp_path / "lane"),
            step="y" * 500,
            last_test_result="z" * 1000,
            session_id="s" * 500,
        )
        assert len(checkpoint.changed_files) == 50
        assert all(len(path) <= 200 for path in checkpoint.changed_files)
        assert len(checkpoint.last_step) == 200
        assert len(checkpoint.last_test_result) == 500
        assert checkpoint.session_id is not None
        assert len(checkpoint.session_id) == 128

    def test_lane_identity_binds_branch_and_path(self) -> None:
        assert lane_identity("1-auto-impl", "/wt") == "1-auto-impl@/wt"

    def test_validate_rejects_mismatches(self, tmp_path: Path) -> None:
        lane = _lane(tmp_path)
        worktree = str(tmp_path / "lane")
        _write_test_checkpoint(lane, worktree=worktree)
        raw = read_checkpoint(checkpoint_path(lane.git_dir))
        assert raw is not None

        assert validate_checkpoint(raw, issue=2, branch="1-auto-impl", worktree=worktree) is None
        assert validate_checkpoint(raw, issue=1, branch="other", worktree=worktree) is None
        assert validate_checkpoint(raw, issue=1, branch="1-auto-impl", worktree="/other") is None

        for field, bad in (
            ("schema_version", 99),
            ("elapsed_s", -1.0),
            ("elapsed_s", float("nan")),
            ("attempt", 0),
            ("head_sha", "not-a-sha"),
        ):
            broken = dict(raw)
            broken[field] = bad
            restored = validate_checkpoint(broken, issue=1, branch="1-auto-impl", worktree=worktree)
            assert restored is None, f"{field}={bad!r} must not validate"

    def test_discard_best_effort(self, tmp_path: Path) -> None:
        lane = _lane(tmp_path)
        path = checkpoint_path(lane.git_dir)
        discard_checkpoint(path)  # missing file is not an error
        _write_test_checkpoint(lane, worktree=str(tmp_path / "lane"))
        assert path.exists()
        discard_checkpoint(path)
        assert not path.exists()


class TestCollectLane:
    """Lane snapshot collection through the faked Git seam."""

    def test_collect_parses_porcelain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        status = " M a.py\n?? b.py\nR  old.py -> new.py\n"
        monkeypatch.setattr(
            implementer_checkpoint,
            "run_git",
            _fake_run_git(git_dir=tmp_path / "lane" / ".git", status=status),
        )
        lane = collect_lane(tmp_path / "lane", timeout_s=10)
        assert lane.head_sha == HEAD
        assert lane.changed_files == ("a.py", "b.py", "new.py")

    def test_collect_rejects_non_full_sha_head(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            implementer_checkpoint,
            "run_git",
            _fake_run_git(git_dir=tmp_path / "lane" / ".git", head="short-sha"),
        )
        lane = collect_lane(tmp_path / "lane", timeout_s=10)
        assert lane.head_sha is None

    def test_collect_raises_on_git_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _failing_run_git)
        with pytest.raises(subprocess.CalledProcessError):
            collect_lane(tmp_path / "lane", timeout_s=10)


class TestResumeContextFor:
    """Fail-closed resume-context selection for the retry prompt."""

    def _prepared(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        head: str = HEAD,
    ) -> Path:
        worktree = tmp_path / "lane"
        git_dir = worktree / ".git"
        git_dir.mkdir(parents=True)
        lane = LaneSnapshot(git_dir=git_dir, head_sha=HEAD, changed_files=("a.py",))
        _write_test_checkpoint(
            lane, worktree=str(worktree), last_test_result="", step="turn in progress"
        )
        monkeypatch.setattr(
            implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir, head=head)
        )
        return worktree

    def test_disabled_returns_empty_without_git(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising_run_git)
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=0),
            worktree=Path("/wt"),
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert result == ""

    def test_missing_checkpoint_returns_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        git_dir = tmp_path / "lane" / ".git"
        git_dir.mkdir(parents=True)
        monkeypatch.setattr(implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir))
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=300),
            worktree=tmp_path / "lane",
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert result == ""

    def test_valid_checkpoint_renders_advisory_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = self._prepared(tmp_path, monkeypatch)
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=300),
            worktree=worktree,
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert "## Mid-Run Checkpoint From the Previous Turn (Advisory)" in result
        assert "a.py" in result
        assert "turn in progress" in result
        assert "advisory only" in result

    def test_superseded_head_discards_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = self._prepared(tmp_path, monkeypatch, head=OTHER_HEAD)
        path = checkpoint_path(worktree / ".git")
        assert path.exists()
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=300),
            worktree=worktree,
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert result == ""
        assert not path.exists()

    def test_malformed_checkpoint_discards(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        worktree = self._prepared(tmp_path, monkeypatch)
        path = checkpoint_path(worktree / ".git")
        path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=300),
            worktree=worktree,
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert result == ""
        assert not path.exists()

    def test_git_failure_degrades_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _failing_run_git)
        result = resume_context_for(
            config=_config(implementer_checkpoint_interval=300),
            worktree=tmp_path / "lane",
            issue=1,
            branch="1-auto-impl",
            timeout_s=10,
        )
        assert result == ""


class TestObserveInFlight:
    """Per-tick checkpoint writes, progress events, and tick pruning."""

    def _walk(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status: str = "",
    ) -> tuple[dict[JobHandle, WorkItem], Path]:
        worktree = tmp_path / "lane"
        git_dir = worktree / ".git"
        git_dir.mkdir(parents=True)
        monkeypatch.setattr(
            implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir, status=status)
        )
        item = _make_item(worktree=str(worktree), stamp=100.0)
        return {_make_handle(tmp_path): item}, worktree

    def test_first_write_on_dispatch_no_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        in_flight, worktree = self._walk(tmp_path, monkeypatch, status=" M a.py\n")
        ticks: dict[str, tuple[float, float]] = {}
        events: list[tuple[Any, ...]] = []

        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=110.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: events.append(fields),
        )

        raw = read_checkpoint(checkpoint_path(worktree / ".git"))
        assert raw is not None
        assert raw["last_step"] == "turn dispatched"
        assert raw["elapsed_s"] == pytest.approx(10.0)
        assert raw["changed_files"] == ["a.py"]
        assert events == []
        assert ticks == {"key-1": (100.0, 410.0)}

    def test_interval_write_emits_progress_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        in_flight, worktree = self._walk(tmp_path, monkeypatch)
        ticks: dict[str, tuple[float, float]] = {}
        events: list[tuple[Any, ...]] = []

        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=110.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: events.append(fields),
        )
        # Before the interval has passed: no new write, no event.
        before = checkpoint_path(worktree / ".git").read_text(encoding="utf-8")
        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=200.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: events.append(fields),
        )
        assert checkpoint_path(worktree / ".git").read_text(encoding="utf-8") == before
        assert events == []

        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=420.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: events.append(fields),
        )
        assert len(events) == 1
        event, key, payload = events[0]
        assert event == "implementer_progress"
        assert key == "key-1"
        assert payload["elapsed_s"] == pytest.approx(320.0)
        assert payload["files_touched_count"] == 0
        assert payload["last_step"] == "turn in progress"

    def test_disabled_interval_writes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising_run_git)
        item = _make_item(worktree=str(tmp_path / "lane"), stamp=100.0)
        events: list[tuple[Any, ...]] = []
        ticks: dict[str, tuple[float, float]] = {}
        observe_in_flight(
            {_make_handle(tmp_path): item},
            ticks=ticks,
            now=110.0,
            interval_s=0,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: events.append(fields),
        )
        assert events == []
        assert ticks == {}

    def test_tick_entries_pruned_when_item_leaves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        in_flight, _worktree = self._walk(tmp_path, monkeypatch)
        ticks: dict[str, tuple[float, float]] = {}
        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=110.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: None,
        )
        assert "key-1" in ticks

        observe_in_flight(
            {},
            ticks=ticks,
            now=120.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: None,
        )
        assert ticks == {}

    def test_non_implementer_items_skipped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising_run_git)
        unstamped = _make_item(worktree=str(tmp_path / "lane"))
        other_stage = _make_item(
            stage=StageName.PLANNING, worktree=str(tmp_path / "lane"), stamp=100.0
        )
        reviewer_job = _make_handle(tmp_path, session_agent="reviewer")
        reviewer_item = _make_item(worktree=str(tmp_path / "lane"), stamp=100.0)
        no_issue_stamped = _make_item(issue=None, worktree=str(tmp_path / "lane"), stamp=100.0)

        ticks: dict[str, tuple[float, float]] = {}
        observe_in_flight(
            {
                _make_handle(tmp_path): unstamped,
                _make_handle(tmp_path): other_stage,
                reviewer_job: reviewer_item,
                _make_handle(tmp_path): no_issue_stamped,
            },
            ticks=ticks,
            now=110.0,
            interval_s=300,
            item_key=lambda item: "key-x",
            metadata_timeout_s=10,
            emit=lambda *fields: None,
        )
        assert ticks == {}

    def test_new_stamp_restarts_tick_schedule(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        in_flight, worktree = self._walk(tmp_path, monkeypatch)
        ticks: dict[str, tuple[float, float]] = {"key-1": (50.0, 60.0)}
        observe_in_flight(
            in_flight,
            ticks=ticks,
            now=110.0,
            interval_s=300,
            item_key=lambda item: "key-1",
            metadata_timeout_s=10,
            emit=lambda *fields: None,
        )
        # The stale entry (stamp 50) does not match the live stamp (100), so
        # this observation is the first write of the fresh turn.
        assert ticks == {"key-1": (100.0, 410.0)}
        raw = read_checkpoint(checkpoint_path(worktree / ".git"))
        assert raw is not None
        assert raw["last_step"] == "turn dispatched"

    def test_failed_snapshot_degrades_to_warning(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _failing_run_git)
        item = _make_item(worktree=str(tmp_path / "lane"), stamp=100.0)
        ticks: dict[str, tuple[float, float]] = {}
        with caplog.at_level("WARNING"):
            observe_in_flight(
                {_make_handle(tmp_path): item},
                ticks=ticks,
                now=110.0,
                interval_s=300,
                item_key=lambda item: "key-1",
                metadata_timeout_s=10,
                emit=lambda *fields: None,
            )
        assert "progress snapshot failed" in caplog.text
        assert ticks == {"key-1": (100.0, 410.0)}


class TestClassifyAgentCompletion:
    """Checkpoint life-cycle closure on implementer job completion."""

    def _prepared(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> WorkItem:
        worktree = tmp_path / "lane"
        git_dir = worktree / ".git"
        git_dir.mkdir(parents=True)
        monkeypatch.setattr(implementer_checkpoint, "run_git", _fake_run_git(git_dir=git_dir))
        return _make_item(worktree=str(worktree), stamp=100.0)

    def test_timeout_writes_final_checkpoint_with_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = self._prepared(tmp_path, monkeypatch)
        result = JobResult(ok=False, error="timeout", session_id="sess-1")
        classify_agent_completion(
            item,
            result,
            interval_s=300,
            metadata_timeout_s=10,
            now=900.0,
        )
        assert "_impl_checkpoint_turn_start" not in item.payload
        raw = read_checkpoint(checkpoint_path(Path(item.worktree) / ".git"))
        assert raw is not None
        assert raw["last_step"] == "invocation deadline reached (timeout)"
        assert raw["elapsed_s"] == pytest.approx(800.0)
        assert raw["session_id"] == "sess-1"

    def test_success_discards_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = self._prepared(tmp_path, monkeypatch)
        path = checkpoint_path(Path(item.worktree) / ".git")
        path.write_text("{}", encoding="utf-8")
        classify_agent_completion(
            item,
            JobResult(ok=True),
            interval_s=300,
            metadata_timeout_s=10,
            now=900.0,
        )
        assert "_impl_checkpoint_turn_start" not in item.payload
        assert not path.exists()

    def test_non_timeout_error_discards_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        item = self._prepared(tmp_path, monkeypatch)
        path = checkpoint_path(Path(item.worktree) / ".git")
        path.write_text("{}", encoding="utf-8")
        classify_agent_completion(
            item,
            JobResult(ok=False, error="agent_error"),
            interval_s=300,
            metadata_timeout_s=10,
            now=900.0,
        )
        assert "_impl_checkpoint_turn_start" not in item.payload
        assert not path.exists()

    def test_unstamped_turn_is_a_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising_run_git)
        item = _make_item(worktree=str(tmp_path / "lane"))
        classify_agent_completion(
            item,
            JobResult(ok=False, error="timeout"),
            interval_s=300,
            metadata_timeout_s=10,
            now=900.0,
        )
        assert "_impl_checkpoint_turn_start" not in item.payload

    def test_disabled_pops_stamp_without_writes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _raising_run_git)
        item = _make_item(worktree=str(tmp_path / "lane"), stamp=100.0)
        classify_agent_completion(
            item,
            JobResult(ok=False, error="timeout"),
            interval_s=0,
            metadata_timeout_s=10,
            now=900.0,
        )
        assert "_impl_checkpoint_turn_start" not in item.payload

    def test_timeout_snapshot_failure_is_best_effort(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(implementer_checkpoint, "run_git", _failing_run_git)
        item = _make_item(worktree=str(tmp_path / "lane"), stamp=100.0)
        with caplog.at_level("WARNING"):
            classify_agent_completion(
                item,
                JobResult(ok=False, error="timeout"),
                interval_s=300,
                metadata_timeout_s=10,
                now=900.0,
            )
        assert "timeout snapshot failed" in caplog.text


class TestApplyTimeoutSessionContinuity:
    """Timeout-result session-id carry-over rules."""

    def _stamped(self) -> WorkItem:
        return _make_item(worktree="/wt", stamp=100.0)

    def _job(self, *, resume_session_id: str | None = "sess-prior") -> AgentJob:
        return AgentJob(
            repo="test-repo",
            issue=1,
            agent="claude",
            model="model",
            prompt_builder=lambda: "",
            cwd=Path("/wt"),
            timeout_s=60,
            session_agent=AGENT_IMPLEMENTER,
            resume_session_id=resume_session_id,
            descr="implement",
        )

    def test_timeout_carries_the_attempted_resume_id(self) -> None:
        result = JobResult(ok=False, error="timeout")
        continued = apply_timeout_session_continuity(
            self._stamped(), result, job=self._job(), interval_s=300
        )
        assert continued.session_id == "sess-prior"

    def test_disabled_leaves_result_unchanged(self) -> None:
        result = JobResult(ok=False, error="timeout")
        assert (
            apply_timeout_session_continuity(self._stamped(), result, job=self._job(), interval_s=0)
            is result
        )

    def test_non_timeout_leaves_result_unchanged(self) -> None:
        result = JobResult(ok=False, error="agent_error")
        assert (
            apply_timeout_session_continuity(
                self._stamped(), result, job=self._job(), interval_s=300
            )
            is result
        )

    def test_reported_session_id_wins(self) -> None:
        result = JobResult(ok=False, error="timeout", session_id="reported")
        assert (
            apply_timeout_session_continuity(
                self._stamped(), result, job=self._job(), interval_s=300
            ).session_id
            == "reported"
        )

    def test_fresh_turn_yields_no_id(self) -> None:
        result = JobResult(ok=False, error="timeout")
        assert (
            apply_timeout_session_continuity(
                self._stamped(), result, job=self._job(resume_session_id=None), interval_s=300
            ).session_id
            is None
        )

    def test_unstamped_turn_unchanged(self) -> None:
        item = _make_item(worktree="/wt")
        result = JobResult(ok=False, error="timeout")
        assert (
            apply_timeout_session_continuity(item, result, job=self._job(), interval_s=300)
            is result
        )
