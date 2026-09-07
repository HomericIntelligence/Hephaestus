"""Tests for remediation operation-wide monotonic deadlines."""

from __future__ import annotations

import queue
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime, git_utils
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    DeliverReplyHandoffRequest,
    FrozenJson,
    GitHubJob,
    RecoverRemediationReplyJournalRequest,
)
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.automation.review_journal import CommentJournalReadError
from hephaestus.utils.file_lock import LockUnavailableError


def test_git_runtime_refuses_commit_after_the_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expired operation must not start another Git child."""
    child = Mock()
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: 11.0)

    with (
        git_runtime.operation_deadline(10.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        git_runtime.run(["git", "commit"], cwd=tmp_path, timeout=120)

    child.assert_not_called()


def test_git_job_deadline_includes_repository_lock_admission(tmp_path: Path) -> None:
    """An expired Git job must not dispatch after repository lock admission."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    dispatch = Mock()
    pool._dispatch_git_op = dispatch  # type: ignore[method-assign]
    try:
        result = pool._run_git(
            GitJob(
                "repo",
                "prepare_remediation_recovery",
                120,
                deadline_s=1.0,
            )
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert not result.ok
    assert result.error == "lock_timeout"
    dispatch.assert_not_called()


def test_publication_starts_neither_push_nor_probe_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An expired publication must not start its push or uncertainty probe."""
    child = Mock()
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: 11.0)

    with (
        git_runtime.operation_deadline(10.0),
        pytest.raises(git_utils.DetachedHeadPushRemoteProbeError) as raised,
    ):
        git_utils.push_head_to_branch(
            "fix/one",
            "a" * 40,
            tmp_path,
            source_sha="b" * 40,
            timeout=120,
        )

    assert raised.value.failure_kind == "timeout"
    child.assert_not_called()


def test_failed_push_does_not_probe_after_the_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed push must not start its uncertainty probe after expiry."""
    child = Mock(side_effect=subprocess.CalledProcessError(1, ["git", "push"]))
    monkeypatch.setattr(git_runtime, "_shared_run_git", child)
    clock = iter((1.0, 3.0))
    monkeypatch.setattr("hephaestus.automation.git_runtime.time.monotonic", lambda: next(clock))

    with (
        git_runtime.operation_deadline(2.0),
        pytest.raises(git_utils.DetachedHeadPushRemoteProbeError) as raised,
    ):
        git_utils.push_head_to_branch(
            "fix/one",
            "a" * 40,
            tmp_path,
            source_sha="b" * 40,
            timeout=120,
        )

    assert raised.value.failure_kind == "timeout"
    assert child.call_count == 1


def test_remediation_requests_reject_invalid_absolute_deadlines() -> None:
    """Closed remediation requests accept only finite positive deadlines."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    body = f'{marker}\n<!-- {{"format":1}} -->'

    with pytest.raises(ValueError, match="deadline_s"):
        AppendReplyJournalRequest(7, marker, body, deadline_s=float("inf"))
    with pytest.raises(ValueError, match="deadline_s"):
        DeliverReplyHandoffRequest(
            3,
            7,
            FrozenJson.snapshot({}),
            0,
            deadline_s=0.0,
        )
    with pytest.raises(ValueError, match="deadline_s"):
        GitJob("repo", "prepare_remediation_recovery", 120, deadline_s=float("nan"))


@pytest.mark.parametrize(
    ("clock_values", "expected_phases", "expected_children"),
    [
        ((1.0, 11.0), ["pre"], 0),
        ((1.0, 2.0, 11.0), ["pre"], 1),
        ((1.0, 2.0, 3.0, 11.0), ["pre", "write", "post"], 2),
    ],
    ids=("pre-read", "write", "post-read"),
)
def test_append_checks_deadline_before_pre_read_write_and_post_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    clock_values: tuple[float, ...],
    expected_phases: list[str],
    expected_children: int,
) -> None:
    """The append operation applies one deadline to all three phases."""
    module = __import__(
        "hephaestus.automation.pipeline_github_jobs",
        fromlist=["PipelineGitHubJobRunner"],
    )
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(
        7,
        marker,
        f'{marker}\n<!-- {{"format":1}} -->',
        deadline_s=10.0,
    )
    facade = __import__("hephaestus.automation.pipeline_github", fromlist=["gh_call"])
    transport = __import__("hephaestus.automation.pipeline_github_transport", fromlist=["time"])
    queries = __import__("hephaestus.automation.pipeline_github_queries", fromlist=["github_api"])
    phases: list[str] = []

    def fetch_comments(
        _issue_number: int,
        *,
        owner: str,
        name: str,
        call: object,
    ) -> list[dict[str, object]]:
        del owner, name
        phases.append("pre" if not phases else "post")
        call(["api", "comments"])  # type: ignore[operator]
        return []

    def gh_call(_argv: list[str], **_kwargs: object) -> SimpleNamespace:
        phases.append("write") if _argv[:3] == ["issue", "comment", "7"] else None
        return SimpleNamespace(stdout="[]", stderr="", returncode=0)

    child = Mock(side_effect=gh_call)
    monkeypatch.setattr(facade, "gh_call", child)
    monkeypatch.setattr(queries.github_api, "_fetch_issue_comments_paginated", fetch_comments)
    clock = iter(clock_values)
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(subprocess.TimeoutExpired):
        module.PipelineGitHubJobRunner("org", False).run(
            GitHubJob("repo", tmp_path.resolve(), request, "append")
        )

    assert phases == expected_phases
    assert child.call_count == expected_children


def test_delivery_checks_deadline_before_each_thread_operation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Reply delivery cannot start a later thread operation after expiry."""
    module = __import__(
        "hephaestus.automation.pipeline_github_jobs",
        fromlist=["PipelineGitHubJobRunner"],
    )
    request = DeliverReplyHandoffRequest(
        3,
        7,
        FrozenJson.snapshot({"format": 1}),
        0,
        deadline_s=10.0,
    )

    facade = __import__("hephaestus.automation.pipeline_github", fromlist=["gh_call"])
    transport = __import__("hephaestus.automation.pipeline_github_transport", fromlist=["time"])
    calls: list[str] = []

    def fake_attempt(_request: object, github: object) -> object:
        for thread_id in ("T1", "T2", "T3"):
            calls.append(thread_id)
            github._gh(["api", thread_id])  # type: ignore[attr-defined]
        return SimpleNamespace()

    child = Mock(return_value=SimpleNamespace(stdout="", stderr="", returncode=0))
    monkeypatch.setattr(module, "attempt_reply_handoff", fake_attempt)
    monkeypatch.setattr(facade, "gh_call", child)
    clock = iter((1.0, 2.0, 11.0))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(subprocess.TimeoutExpired):
        module.PipelineGitHubJobRunner("org", False).run(
            GitHubJob("repo", tmp_path.resolve(), request, "deliver")
        )

    assert calls == ["T1", "T2", "T3"]
    assert child.call_count == 2


def test_delivery_file_lock_wait_stops_at_operation_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The PR reply lock wait cannot consume time after delivery expires."""
    facade = __import__("hephaestus.automation.pipeline_github", fromlist=["file_lock"])
    transport = __import__("hephaestus.automation.pipeline_github_transport", fromlist=["time"])
    adapter = facade.PipelineGitHub("org", repo="repo", repo_root=tmp_path)

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise LockUnavailableError("held")

    monkeypatch.setattr(facade, "file_lock", unavailable)
    monkeypatch.setattr(transport.time, "sleep", lambda _seconds: None)
    clock = iter((1.0, 11.0))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with (
        adapter.operation_deadline(10.0),
        pytest.raises(subprocess.TimeoutExpired),
    ):
        with adapter._operation_file_lock(tmp_path / "reply.lock", require_exclusive=True):
            raise AssertionError("the held lock must not be acquired")


def _recovery_request(deadline_s: float) -> RecoverRemediationReplyJournalRequest:
    """Build one closed format-3 recovery read request."""
    return RecoverRemediationReplyJournalRequest(
        issue_number=3,
        pr_number=7,
        repository="org/repo",
        branch="fix/recovery",
        current_remote_head="a" * 40,
        threads=FrozenJson.snapshot([]),
        deadline_s=deadline_s,
    )


def test_recovery_deadline_includes_repository_lock_admission(tmp_path: Path) -> None:
    """An expired recovery read must not dispatch after its repository lock."""
    runner = Mock()
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        github_job_runner=runner,
    )
    try:
        result = pool._run_github(
            GitHubJob("repo", tmp_path.resolve(), _recovery_request(1.0), "recover")
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert not result.ok
    assert result.error == "github_timeout"
    runner.run.assert_not_called()


def test_recovery_pagination_stops_when_aggregate_deadline_expires(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A later recovery page cannot receive a new GitHub timeout budget."""
    module = __import__(
        "hephaestus.automation.pipeline_github_jobs",
        fromlist=["PipelineGitHubJobRunner"],
    )
    facade = __import__("hephaestus.automation.pipeline_github", fromlist=["gh_call"])
    transport = __import__("hephaestus.automation.pipeline_github_transport", fromlist=["time"])
    queries = __import__("hephaestus.automation.pipeline_github_queries", fromlist=["github_api"])
    child = Mock(return_value=SimpleNamespace(stdout="[]", stderr="", returncode=0))

    def fetch_comments(
        _issue_number: int, *, owner: str, name: str, call: object
    ) -> list[dict[str, object]]:
        del owner, name
        call(["api", "page-1"])  # type: ignore[operator]
        call(["api", "page-2"])  # type: ignore[operator]
        return []

    monkeypatch.setattr(facade, "gh_call", child)
    monkeypatch.setattr(queries.github_api, "_fetch_issue_comments_paginated", fetch_comments)
    clock = iter((1.0, 11.0))
    monkeypatch.setattr(transport.time, "monotonic", lambda: next(clock))

    with pytest.raises(CommentJournalReadError, match="deadline"):
        module.PipelineGitHubJobRunner("org", False).run(
            GitHubJob("repo", tmp_path.resolve(), _recovery_request(10.0), "recover")
        )

    assert child.call_count == 1
