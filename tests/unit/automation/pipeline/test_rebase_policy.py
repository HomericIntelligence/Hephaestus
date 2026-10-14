"""Check the host rebase admission policy."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event
from time import monotonic
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from tests.unit.automation.pipeline.conftest import FakeSigningProvider
from tests.unit.automation.test_rebase_recovery import (
    _abort_worker_case,
    _automatic_local_worker_case,
    _normal_publication_job,
    _publication_validation_seams,
    _store,
)
from tests.unit.automation.test_source_worktree import _git

WP = "hephaestus.automation.pipeline.worker_pool"


def _observe_fetched_base(case: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Check the captured base at the external Git mutation boundary."""
    from hephaestus.automation import git_utils

    rebase = git_utils.rebase_worktree_onto

    def captured(**kwargs: Any) -> bool:
        assert kwargs["base_sha"] == case.base
        return bool(rebase(**kwargs))

    monkeypatch.setattr(git_utils, "rebase_worktree_onto", captured)


@contextmanager
def real_policy_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, publish: bool, reason: str = "manual"
) -> Iterator[Any]:
    """Reuse real source ownership and retain only external execution substitutes."""
    from hephaestus.automation import git_utils
    from hephaestus.automation.pipeline.github_jobs import (
        InspectRebaseConflictRequest,
        InspectRebaseReviewRequest,
        RebaseConflictInspected,
        RebaseReviewInspected,
        RebaseReviewPublished,
    )

    with _abort_worker_case(
        tmp_path,
        monkeypatch,
        fallback=False,
        fault="clean",
        conflict=False,
        structural=True,
        pr_number=1001 if publish else None,
    ) as case:
        case.job = replace(case.job, kwargs={**case.job.kwargs, "rebase_reason": reason})
        case.checks = _publication_validation_seams(case, "initial", monkeypatch)
        _observe_fetched_base(case, monkeypatch)
        case.events = []
        case.remote_head = case.original
        case.publication_ok = True
        case.remote_drift = False
        case.merge_tree_results = []
        run_git = git_utils.run

        def observe_git(argv: list[str], **kwargs: Any) -> Any:
            result = run_git(argv, **kwargs)
            if argv[:2] == ["git", "merge-tree"]:
                case.merge_tree_results.append(
                    (tuple(argv), result.returncode, result.stdout, result.stderr)
                )
            return result

        monkeypatch.setattr(git_utils, "run", observe_git)

        def read_remote(cwd: Path, **kwargs: Any) -> str:
            assert cwd == case.binding.cwd
            assert kwargs["expected_repo"] == "acme/repository"
            if kwargs["branch"] == "main":
                return case.base
            assert kwargs["branch"] == "1-repair"
            return case.remote_head

        def transport(job: Any, **kwargs: Any) -> Any:
            request = job.request
            if isinstance(request, InspectRebaseConflictRequest):
                assert request.reviewed_head_sha == case.original
                assert request.base_sha == case.base
                return RebaseConflictInspected(request, True, "Controlled live admission.")
            if isinstance(request, InspectRebaseReviewRequest):
                return RebaseReviewInspected(request, True)
            case.events.append("record")
            return RebaseReviewPublished(request, case.publication_ok)

        def push(branch: str, expected: str, cwd: Path, **kwargs: Any) -> None:
            assert branch == "1-repair" and expected == case.original
            assert cwd == case.binding.cwd
            record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
            assert record.phase == "publication_intent"
            assert record.remote_head_sha == expected
            assert kwargs["source_sha"] == record.resulting_workspace.revision
            case.events.append("push")
            case.remote_head = case.original if case.remote_drift else kwargs["source_sha"]

        case.pool._github_job_runner = MagicMock(gh_timeout=60, run=transport)
        monkeypatch.setattr(case.pool, "_read_remote_branch_head", read_remote)
        monkeypatch.setattr(git_utils, "push_head_to_branch", push)
        yield case


@pytest.fixture
def worker_factory(tmp_path: Path) -> Iterator[Callable[[], WorkerPool]]:
    """Create test workers and close them after the test."""
    workers: list[WorkerPool] = []

    def create() -> WorkerPool:
        worker = WorkerPool(
            size=1,
            shutdown=Event(),
            completion_q=CompletionQueue(),
            lock_dir=tmp_path / "worker-locks",
            host_capabilities=WorkerCapabilities(None, "unit-worker", FakeSigningProvider()),
        )
        workers.append(worker)
        return worker

    try:
        yield create
    finally:
        for worker in workers:
            worker.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("reason", [None, "", "behind", "dependency_sync"])
def test_rebase_requires_allowed_reason(
    tmp_path: Path, reason: str | None, worker_factory: Callable[[], WorkerPool]
) -> None:
    """An unapproved reason must not start Git work."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={"cwd": tmp_path, "rebase_reason": reason},
    )
    with patch.object(pool, "_authenticated_remote_revalidator") as remote:
        result = pool._git_rebase(job, record_source=MagicMock())
    assert not result.ok
    assert result.error == "rebase reason is not allowed"
    remote.assert_not_called()


def test_publication_refresh_does_not_rebase(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A changed remote head must leave local history unchanged."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="commit_push",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "writer_refresh": {
                "phase": "rebase",
                "source_sha": "a" * 40,
                "expected_remote_sha": "b" * 40,
            }
        },
    )
    with patch(f"{WP}.git_utils.run") as run:
        result = pool._refresh_writer_publication(job, tmp_path, "issue-branch")
    assert not result.ok
    assert result.value == {"writer_refresh_failure": "remote_changed"}
    run.assert_not_called()


@pytest.mark.parametrize(
    "reason,publish",
    [("implementation_start", False), ("review_conflict", True), ("manual", True)],
)
def test_admitted_rebase_uses_fetched_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    publish: bool,
) -> None:
    """Use the captured base under real source ownership and the exact publication lease."""
    if reason == "implementation_start":
        with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=False) as case:
            _publication_validation_seams(case, "initial", monkeypatch)
            _observe_fetched_base(case, monkeypatch)
            result = case.pool._run_git(case.job)
            assert result.ok, result
            assert result.value["implementation_started"] is True
            case.pushes.assert_not_called()
            assert result.value["published"] is False
            assert _git(case.binding.cwd, "merge-base", "--is-ancestor", case.base, "HEAD") == ""
            assert case.starts == [True]
        return
    with real_policy_case(tmp_path, monkeypatch, publish=publish, reason=reason) as case:
        result = case.pool._run_git(case.job)
        assert result.ok, result
        assert result.value["rebased"] is True
        assert result.value["published"] is publish
        assert result.value["head_sha"] == _git(case.binding.cwd, "rev-parse", "HEAD")
        assert _git(case.binding.cwd, "merge-base", "--is-ancestor", case.base, "HEAD") == ""
        assert case.starts == [True]
        assert case.events == ["push"]


def test_manual_conflict_aborts_before_agent_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Require a checked real abort and retained restoration before a fresh restart."""
    with _abort_worker_case(
        tmp_path,
        monkeypatch,
        fallback=False,
        fault="clean",
        pr_number=None,
    ) as case:
        result = case.pool._run_git(case.job)
        assert result.error == "rebase conflict restart required", result
        assert result.value["rebase_restart_required"] is True
        assert result.value["base_sha"] == case.base
        assert result.value["head_sha"] == case.original
        assert case.starts == [True] and case.aborts == [1]
        case.pushes.assert_not_called()
        record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert record.phase == "aborted"
        assert record.restored_workspace == case.binding
        assert record.restored_tree_sha == case.tree
        assert _git(case.binding.cwd, "rev-parse", "HEAD") == case.original
        assert _git(case.binding.cwd, "status", "--porcelain") == ""


@pytest.mark.parametrize("moved", ["source", "base"])
def test_conflict_restart_rejects_changed_input(
    tmp_path: Path, moved: str, worker_factory: Callable[[], WorkerPool]
) -> None:
    """A changed source or base must stop the replay."""
    from hephaestus.automation.pipeline.jobs import JobResult

    pool = worker_factory()
    head, base, changed = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="test/repo",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "rebase_reason": "manual",
            "expected_head_sha": head,
            "expected_base_sha": base,
            "resolve_conflicts": True,
        },
    )
    with (
        patch.object(
            pool,
            "_git_fetch_main",
            return_value=JobResult(
                ok=True, value={"head_sha": changed if moved == "base" else base}
            ),
        ),
        patch.object(
            pool, "_read_publish_head", return_value=changed if moved == "source" else head
        ),
        patch(f"{WP}.git_utils.is_clean_working_tree", return_value=True),
        patch(f"{WP}.git_utils.rebase_worktree_onto") as rebase,
    ):
        result = pool._git_rebase_once(job, record_source=MagicMock())
    assert not result.ok
    rebase.assert_not_called()


def test_fetch_main_does_not_change_checkout(
    tmp_path: Path, worker_factory: Callable[[], WorkerPool]
) -> None:
    """Fetch only the main ref and read its fetched commit."""
    pool = worker_factory()
    job = GitJob(
        repo="test/repo",
        op="fetch_main",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={"cwd": tmp_path},
    )
    with (
        patch.object(pool, "_authenticated_remote_git_configuration", return_value=({}, ())),
        patch(f"{WP}.git_utils.run", return_value=MagicMock(stdout="a" * 40)) as run,
    ):
        result = pool._git_fetch_main(job)
    assert result.ok
    assert result.value == {"head_sha": "a" * 40}
    assert [c.args[0] for c in run.call_args_list] == [
        ["git", "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        ["git", "rev-parse", "--verify", "FETCH_HEAD^{commit}"],
    ]


def test_initial_conflict_continuation_does_not_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Continue a real accepted local conflict without publishing the writer."""
    with _automatic_local_worker_case(tmp_path, monkeypatch, conflict=True) as case:
        _publication_validation_seams(case, "continued", monkeypatch)
        job = _normal_publication_job(case, "continued")
        assert job.op == "continue_rebase"
        assert job.capability_target.request_id != case.job.capability_target.request_id
        assert job.kwargs["rebase_recovery_intent_id"] == case.job.capability_target.request_id
        intent = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert intent.phase == "intent" and intent.request == case.job.capability_target
        read_remote = case.pool._read_remote_branch_head

        def read_base(cwd: Path, **kwargs: Any) -> Any:
            assert kwargs["branch"] == "main"
            assert kwargs["expected_repo"] == "acme/repository"
            return read_remote(cwd, **kwargs)

        with patch.object(case.pool, "_read_remote_branch_head", side_effect=read_base) as remote:
            result = case.pool._run_git(job)
        assert result.ok, result
        assert result.value["published"] is False
        assert result.value["head_sha"] == _git(case.binding.cwd, "rev-parse", "HEAD")
        assert remote.call_count > 0
        case.pushes.assert_not_called()


def _reviewed_policy_job(case: Any, failure: str | None = None) -> GitJob:
    """Bind the review to real Git objects, without replacing tree verification."""
    from hephaestus.automation.review_audit import ReviewAudit

    reviewed_base = _git(case.binding.cwd, "merge-base", case.original, case.base)
    if failure == "tree_changed":
        # This reviewed empty change cannot authorize the writer's replayed changes.
        reviewed_base = case.original
    return replace(
        case.job,
        kwargs={
            **case.job.kwargs,
            "reviewed_head_sha": case.original,
            "reviewed_base_sha": reviewed_base,
            "review_audit": (
                None
                if failure == "audit_missing"
                else ReviewAudit("A", "Checks passed.", (), "", True, "GO")
            ),
        },
    )


def _assert_supported_tree_comparison(case: Any) -> None:
    """Require real tree execution before interpreting the review result."""
    assert case.merge_tree_results, "The worker did not run the tree comparison."
    assert all(result[1] == 0 for result in case.merge_tree_results), case.merge_tree_results


@pytest.mark.parametrize("failure", [None, "tree_changed", "remote_changed", "audit_missing"])
def test_published_rebase_returns_separate_review_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str | None,
) -> None:
    """Keep the reviewed head separate and reject each specified proof failure."""
    with real_policy_case(
        tmp_path,
        monkeypatch,
        publish=True,
        reason="review_conflict",
    ) as case:
        case.job = _reviewed_policy_job(case, failure)
        case.remote_drift = failure == "remote_changed"
        result = case.pool._run_git(case.job)
        assert case.starts == [True]
        if failure != "audit_missing":
            _assert_supported_tree_comparison(case)
        else:
            assert case.merge_tree_results == []
        record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert record.resulting_workspace is not None
        rewritten = record.resulting_workspace.revision
        assert rewritten != case.original
        expected_errors = {
            "tree_changed": "rebase tree changed; source decision required",
            "remote_changed": "rebase publication is unverified",
            "audit_missing": "initial rebase review audit is invalid",
        }
        if failure is not None:
            assert not result.ok, result
            assert result.error == expected_errors[failure]
            assert "retained_rebase_review_proof" not in result.value
            assert case.events == (["record", "push"] if failure == "remote_changed" else [])
            return
        assert result.ok, result
        proof = result.value["retained_rebase_review_proof"]
        assert proof.reviewed_head_sha == case.original
        assert proof.resulting_head_sha == rewritten
        assert proof.resulting_tree_sha == _git(case.binding.cwd, "rev-parse", "HEAD^{tree}")
        assert proof.target_base_sha == case.base
        assert case.events == ["record", "push"]


@pytest.mark.parametrize("publication_ok", [True, False])
def test_review_record_is_visible_before_the_rebase_push(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publication_ok: bool,
) -> None:
    """A rejected record publication must stop the exact-source branch push."""
    with real_policy_case(
        tmp_path,
        monkeypatch,
        publish=True,
        reason="review_conflict",
    ) as case:
        case.job = _reviewed_policy_job(case)
        case.publication_ok = publication_ok
        result = case.pool._run_git(case.job)
        assert case.starts == [True]
        _assert_supported_tree_comparison(case)
        assert result.ok is publication_ok, result
        assert case.events == (["record", "push"] if publication_ok else ["record"])
        if not publication_ok:
            assert result.error == "rebase record publication failed"
            assert case.remote_head == case.original
