"""Tests for the WorkerPool job execution."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import nullcontext
from functools import partial
from pathlib import Path
from typing import Any, cast
from unittest.mock import ANY, MagicMock, call, patch

import pytest

from hephaestus.agents import runtime as agent_runtime
from hephaestus.agents.execution_policy import (
    AgentOperation,
    AgentRole,
    ExecutionPolicy,
    ExecutionRequest,
    SessionLifecycle,
)
from hephaestus.agents.pi_plugins import InventoryResult, PiPreflightResult
from hephaestus.agents.pi_session import create_pi_binding
from hephaestus.agents.runtime import AgentExecutionError, AgentRunResult
from hephaestus.automation import git_utils, subprocess_registry
from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.github_jobs import (
    AppendReplyJournalRequest,
    GitHubJob,
    ReplyJournalAppended,
)
from hephaestus.automation.pipeline.jobs import (
    WORKTREE_MATERIALIZED_KEY,
    AgentJob,
    BuildTestJob,
    CompactJob,
    GitJob,
    JobHandle,
    JobResult,
)
from hephaestus.automation.pipeline.queues import CompletionQueue
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.worker_pool import (
    WorkerPool,
    _confirmed_pytest_failure,
    _controlled_git_signing_env,
    _dirty_worktree_content_snapshot,
    _hdiutil_create_argv,
    _host_validation_failure_kind,
    _host_verification_command,
    _host_verification_env,
    _host_verification_profile,
    _prepare_host_output_aliases,
    _quota_backed_volume,
    _repo_lock_path,
    _run_bounded_host_command,
    _trusted_gh_executable,
    _trusted_git_executable,
    _unsafe_local_git_config_key,
    _validated_signing_key,
    _verifier_owned_runtime_environment,
)
from hephaestus.automation.prompts.pr_review import PrReviewPromptSizeError
from hephaestus.automation.review_journal import CommentJournalReadError
from hephaestus.automation.session_naming import (
    AGENT_IMPLEMENTER,
    AGENT_PR_REVIEWER,
)
from hephaestus.automation.source_worktree import SourceWorkspaceError
from hephaestus.automation.worktree_manager import (
    BRANCH_WORKTREE_OWNED,
    BranchWorktreeOwnedError,
    ImplementationWriterAuthority,
    WorktreeCreationReceiptError,
)
from hephaestus.github.client import GitHubRateLimitError, GitHubUnavailableError
from hephaestus.prompts import PromptCatalog
from hephaestus.resilience import CircuitBreakerOpenError, get_circuit_breaker
from hephaestus.utils.file_lock import LockUnavailableError, file_lock
from hephaestus.utils.helpers import get_repo_root


@pytest.fixture(autouse=True)
def codex_automation_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give mocked Codex processes the minimal admitted profile source."""
    source_home = tmp_path / "codex-home"
    (source_home / "plugins" / "cache" / "athena").mkdir(parents=True)
    (source_home / "auth.json").write_text('{"auth": "test"}\n', encoding="utf-8")
    monkeypatch.setattr(
        agent_runtime,
        "_codex_child_env",
        lambda: {"PATH": os.defpath, "CODEX_HOME": str(source_home)},
    )


WRITING_STANDARD_SENTINEL = "ASD-STE100 Simplified Technical English, Issue 9"

_WP = "hephaestus.automation.pipeline.worker_pool"
_TEST_AGENT_CWD = Path(__file__).resolve().parents[4] / "build" / "worker-pool-tests"
_DIRTY_CONTENT_SNAPSHOT = {
    "index_sha256": "1" * 64,
    "worktree_sha256": "2" * 64,
    "untracked_sha256": "3" * 64,
}


def test_worker_persists_pi_session_and_resolved_policy_receipt(tmp_path: Path) -> None:
    """Opt-in evidence records the queue result without exposing provider output."""
    receipt_dir = tmp_path / "receipts"
    request = ExecutionRequest(
        AgentRole.PLANNER,
        AgentOperation.PLAN,
        SessionLifecycle.START_NEW,
    )
    job = AgentJob(
        repo="Hephaestus",
        issue=2519,
        agent="pi",
        model="pi-model",
        prompt_builder=lambda: "private prompt",
        cwd=tmp_path,
        timeout_s=60,
        execution_request=request,
        descr="plan",
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        evidence_receipt_dir=receipt_dir,
    )
    try:
        with patch.object(
            pool,
            "_run_agent",
            return_value=JobResult(ok=True, session_id="pi-session-2519"),
        ):
            result = pool._run(job, claim_key="Hephaestus#2519", claim_stage="planning")
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True
    receipts = list(receipt_dir.glob("*.json"))
    assert len(receipts) == 1
    payload = json.loads(receipts[0].read_text())
    assert payload["job_type"] == "agent"
    assert payload["provider"] == "pi"
    assert payload["session_id"] == "pi-session-2519"
    assert payload["execution_request"] == {
        "role": "planner",
        "operation": "plan",
        "lifecycle": "start_new",
    }
    assert payload["tool_scopes"] == ["find", "grep", "ls", "read"]
    assert "private prompt" not in receipts[0].read_text()


def test_evidence_receipts_cover_host_lifecycle_jobs(tmp_path: Path) -> None:
    """The private sink records build, Git, and GitHub lifecycle boundaries."""
    receipt_dir = tmp_path / "receipts"
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path,
        evidence_receipt_dir=receipt_dir,
    )
    jobs: list[BuildTestJob | GitJob] = [
        BuildTestJob("Hephaestus", tmp_path, ("uv", "run", "pytest"), 60, descr="tests"),
        GitJob("Hephaestus", "push", 60, descr="push"),
    ]
    try:
        for job in jobs:
            pool._persist_evidence_receipt(
                job,
                JobResult(ok=True),
                "Hephaestus#2519",
                "implementation",
            )
    finally:
        pool.shutdown(mark_interrupted=False)

    payloads = [json.loads(path.read_text()) for path in receipt_dir.glob("*.json")]
    assert {payload["job_type"] for payload in payloads} == {"build_test", "git"}
    assert all(payload["claim_key"] == "Hephaestus#2519" for payload in payloads)
    assert all(payload["claim_stage"] == "implementation" for payload in payloads)
    assert all(payload["interrupted"] is False for payload in payloads)


def _executable_path(name: str, *, path: str | None = None) -> str:
    """Resolve an executable expected to be available in this test environment."""
    executable = shutil.which(name, path=path)
    assert executable is not None
    return str(Path(executable).resolve())


def test_trusted_gh_executable_accepts_explicit_extra_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit root contributes only its contained ``bin/gh`` executable."""
    gh_root = tmp_path / "custom-gh"
    executable = gh_root / "bin" / "gh"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    monkeypatch.setattr(f"{_WP}._TRUSTED_GH_CANDIDATES", ())

    assert _trusted_gh_executable(gh_root) == str(executable)


def test_trusted_git_executable_accepts_discovered_binary_in_fixed_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A directly discovered package-manager Git remains inside a fixed root."""
    executable = Path("/opt/homebrew/Cellar/git/test/bin/git")
    monkeypatch.setattr(f"{_WP}.shutil.which", lambda _name: str(executable))
    monkeypatch.setattr(f"{_WP}.Path.stat", lambda _self: MagicMock(st_mode=0o100555))
    monkeypatch.setattr(f"{_WP}.Path.is_file", lambda _self: True)
    monkeypatch.setattr(f"{_WP}.Path.is_symlink", lambda _self: False)
    monkeypatch.setattr(f"{_WP}.os.access", lambda _path, _mode: True)

    assert _trusted_git_executable() == str(executable)


def test_trusted_git_executable_rejects_discovered_binary_outside_fixed_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller-controlled PATH cannot introduce an arbitrary Git executable."""
    executable = tmp_path / "git"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o555)
    monkeypatch.setattr(f"{_WP}.shutil.which", lambda _name: str(executable))
    monkeypatch.setattr(f"{_WP}._TRUSTED_GIT_CANDIDATES", ())

    assert _trusted_git_executable() is None


def test_controlled_git_signing_env_reinjects_only_validated_identity(
    tmp_path: Path,
) -> None:
    """A policy rebase keeps signing identity without restoring ambient config."""
    signing = {
        "user.name": "Test User",
        "user.email": "test@example.invalid",
        "gpg.format": "ssh",
        "user.signingkey": str(tmp_path / "signing-key"),
    }
    with patch(
        f"{_WP}._read_host_git_signing_config",
        return_value=signing,
        create=True,
    ):
        env = _controlled_git_signing_env(tmp_path, timeout=60)

    assert isinstance(env, dict)
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_COUNT"] == "6"
    injected = {
        env[f"GIT_CONFIG_KEY_{index}"]: env[f"GIT_CONFIG_VALUE_{index}"] for index in range(6)
    }
    assert injected == {
        **signing,
        "commit.gpgsign": "true",
        "gpg.ssh.program": "/usr/bin/ssh-keygen",
    }


@pytest.mark.parametrize("value", ["~no_such_signing_user/key", "key\x00suffix"])
def test_validated_signing_key_rejects_malformed_paths(value: str) -> None:
    """Malformed host signing-key configuration fails closed without a crash."""
    assert _validated_signing_key(value) is None


@pytest.fixture
def shutdown_event() -> threading.Event:
    """Fresh shutdown event for each test."""
    return threading.Event()


@pytest.fixture
def pool(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> Iterator[WorkerPool]:
    """Worker pool with a single thread and a temp cross-process lock dir.

    Injects the repository-owned ADR rebase policy so the ADR semantic and
    structural validation gates behave as they do in the production
    coordinator wiring.  A deliberately policy-free pool is exercised by the
    unaffected-layout regression tests below.
    """
    from hephaestus.automation.pipeline.rebase_adr_policy import (
        select_rebase_policy,
    )

    p = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        rebase_policy_selector=partial(select_rebase_policy, "HomericIntelligence"),
    )
    yield p
    p.shutdown()


def _agent_job(model: str = "opus-4-8", **overrides: object) -> AgentJob:
    """Build an AgentJob with test defaults.

    Failing-path tests pass a unique ``model`` to keep their invocation
    details distinct while the runtime circuit breaker remains shared.
    """
    _TEST_AGENT_CWD.mkdir(parents=True, exist_ok=True)
    defaults: dict[str, object] = {
        "repo": "test/repo",
        "issue": 123,
        "agent": "claude",
        "model": model,
        "prompt_builder": lambda: "test prompt",
        "cwd": _TEST_AGENT_CWD,
        "timeout_s": 60,
        "descr": "test job",
    }
    defaults.update(overrides)
    return AgentJob(**defaults)  # type: ignore[arg-type]


def test_shutdown_can_reap_without_marking_interrupted(
    pool: WorkerPool, shutdown_event: threading.Event
) -> None:
    """Coordinator cleanup may release the pool without changing outcome state (#2431)."""
    pool.shutdown(mark_interrupted=False)

    assert not shutdown_event.is_set()


def test_github_job_dispatches_once_through_injected_typed_runner(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """GitHub jobs have one typed dispatch and no generic worker replay."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(
        issue_number=3,
        marker=marker,
        body=f'{marker}\n<!-- {{"format":1}} -->',
    )
    job = GitHubJob(
        repo="example",
        repo_root=tmp_path.resolve(),
        request=request,
        descr="append journal",
    )
    calls: list[GitHubJob] = []

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            assert isinstance(submitted, GitHubJob)
            calls.append(submitted)
            return ReplyJournalAppended(request=submitted.request)  # type: ignore[arg-type]

    pool = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    try:
        result = pool._run(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok
    assert result.value == ReplyJournalAppended(request=request)
    assert calls == [job]


def test_same_repo_github_jobs_are_serialized(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """The explicit StageGitHub contract permits one in-flight job per repo."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(3, marker, f'{marker}\n<!-- {{"format":1}} -->')
    first_entered = threading.Event()
    second_entered = threading.Event()
    release_first = threading.Event()
    active = 0
    max_active = 0
    guard = threading.Lock()

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            nonlocal active, max_active
            assert isinstance(submitted, GitHubJob)
            with guard:
                active += 1
                max_active = max(max_active, active)
                entered = first_entered if not first_entered.is_set() else second_entered
                entered.set()
            if entered is first_entered:
                assert release_first.wait(timeout=2)
            with guard:
                active -= 1
            return ReplyJournalAppended(request=submitted.request)  # type: ignore[arg-type]

    pool = WorkerPool(
        size=2,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    jobs = [
        GitHubJob("same-repo", tmp_path.resolve(), request, f"append-{index}") for index in range(2)
    ]
    try:
        for job in jobs:
            pool.submit(job, "done")
        assert first_entered.wait(timeout=2)
        assert not second_entered.wait(timeout=0.05)
        release_first.set()
        assert second_entered.wait(timeout=2)
        completion_q.get(timeout=2)
        completion_q.get(timeout=2)
    finally:
        release_first.set()
        pool.shutdown(mark_interrupted=False)

    assert max_active == 1


def test_different_repo_github_jobs_may_run_concurrently(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """Independent repositories do not share the worker-operation lock."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(3, marker, f'{marker}\n<!-- {{"format":1}} -->')
    both_entered = threading.Event()
    release = threading.Event()
    active = 0
    max_active = 0
    guard = threading.Lock()

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            nonlocal active, max_active
            assert isinstance(submitted, GitHubJob)
            with guard:
                active += 1
                max_active = max(max_active, active)
                if active == 2:
                    both_entered.set()
            assert release.wait(timeout=2)
            with guard:
                active -= 1
            return ReplyJournalAppended(request=submitted.request)  # type: ignore[arg-type]

    pool = WorkerPool(
        size=2,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    try:
        pool.submit(GitHubJob("repo-a", tmp_path.resolve(), request, "append-a"), "done")
        pool.submit(GitHubJob("repo-b", tmp_path.resolve(), request, "append-b"), "done")
        assert both_entered.wait(timeout=2)
        release.set()
        completion_q.get(timeout=2)
        completion_q.get(timeout=2)
    finally:
        release.set()
        pool.shutdown(mark_interrupted=False)

    assert max_active == 2


def test_failing_github_job_is_not_replayed_by_worker_pool(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """Mutation ambiguity remains stage-owned and receives one failed result."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(3, marker, f'{marker}\n<!-- {{"format":1}} -->')
    calls = 0

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            nonlocal calls
            assert isinstance(submitted, GitHubJob)
            del submitted
            calls += 1
            raise OSError("ambiguous transport")

    pool = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    try:
        result = pool._run(GitHubJob("repo", tmp_path.resolve(), request, "append"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert not result.ok
    assert "ambiguous transport" in (result.error or "")
    assert calls == 1


@pytest.mark.parametrize(
    ("error", "failure_kind", "retry_delay_s"),
    [
        (GitHubRateLimitError("private rate detail", reset_epoch=145), "github_rate_limit", 45.0),
        (GitHubUnavailableError("private breaker detail"), "github_unavailable", 60.0),
        (CommentJournalReadError("private journal detail"), "comment_journal_read_error", 1.0),
        (
            subprocess.CalledProcessError(1, ["gh"], stderr="private CLI detail"),
            "github_cli_error",
            1.0,
        ),
    ],
)
def test_github_job_returns_safe_structured_failure(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
    error: Exception,
    failure_kind: str,
    retry_delay_s: float,
) -> None:
    """A GitHub failure keeps its class and removes provider details."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(3, marker, f'{marker}\n<!-- {{"format":1}} -->')

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            del submitted
            raise error

    pool = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    try:
        with patch("hephaestus.automation.pipeline.worker_pool.time.time", return_value=100.0):
            result = pool._run(GitHubJob("repo", tmp_path.resolve(), request, "append"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert not result.ok
    assert result.error == failure_kind
    assert result.value == {
        "failure_kind": failure_kind,
        "retry_delay_s": retry_delay_s,
    }
    assert "private" not in repr(result)


def test_github_job_classifies_wrapped_rate_limit(
    shutdown_event: threading.Event,
    completion_q: CompletionQueue,
    tmp_path: Path,
) -> None:
    """A journal error keeps the rate-limit class from its cause."""
    marker = (
        f"<!-- hephaestus-implementation-reply-handoff:pr=7:head={'a' * 40}:batch={'b' * 32} -->"
    )
    request = AppendReplyJournalRequest(3, marker, f'{marker}\n<!-- {{"format":1}} -->')

    class Runner:
        def run(self, submitted: GitHubJob) -> ReplyJournalAppended:
            del submitted
            try:
                raise GitHubRateLimitError("private rate detail", reset_epoch=145)
            except GitHubRateLimitError as exc:
                raise CommentJournalReadError("private journal detail") from exc

    pool = WorkerPool(
        size=1,
        shutdown=shutdown_event,
        completion_q=completion_q,
        lock_dir=tmp_path / "locks",
        github_job_runner=Runner(),
    )
    try:
        with patch("hephaestus.automation.pipeline.worker_pool.time.time", return_value=100.0):
            result = pool._run(GitHubJob("repo", tmp_path.resolve(), request, "append"))
    finally:
        pool.shutdown(mark_interrupted=False)

    assert not result.ok
    assert result.error == "github_rate_limit"
    assert result.value == {
        "failure_kind": "github_rate_limit",
        "retry_delay_s": 45.0,
    }
    assert "private" not in repr(result)


class TestWorkerPoolSubmitComplete:
    """Tests for basic submit/complete workflow."""

    def test_completion_callback_notifies_after_delivering_one_result(
        self,
        shutdown_event: threading.Event,
        tmp_path: Path,
    ) -> None:
        """A completed future delivers one result and wakes its coordinator."""
        completion_q: CompletionQueue = queue.Queue(maxsize=1)
        pool = WorkerPool(
            size=1,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        wakeup = threading.Event()
        saturation = threading.Event()
        pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)
        future: Future[JobResult] = Future()
        result = JobResult(ok=True, value="done")
        future.set_result(result)
        handle = JobHandle(job=_agent_job(), on_done_state=StageName.PLANNING)

        try:
            pool._on_future_done(handle, future)
            delivered_handle, delivered_result = completion_q.get_nowait()
        finally:
            pool.shutdown(mark_interrupted=False)

        assert delivered_handle is handle
        assert delivered_result is result
        assert wakeup.is_set()
        assert not saturation.is_set()

    def test_full_completion_queue_reports_saturation_without_blocking_callback(
        self,
        shutdown_event: threading.Event,
        tmp_path: Path,
    ) -> None:
        """An impossible completion overflow faults the run rather than deadlocking a worker."""
        completion_q: CompletionQueue = queue.Queue(maxsize=1)
        occupied = (object(), JobResult(ok=True, value="already queued"))
        completion_q.put_nowait(occupied)
        pool = WorkerPool(
            size=1,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        wakeup = threading.Event()
        saturation = threading.Event()
        pool.set_completion_notifiers(wakeup=wakeup, saturation=saturation)
        future: Future[JobResult] = Future()
        future.set_result(JobResult(ok=True, value="would overflow"))
        handle = JobHandle(job=_agent_job(), on_done_state=StageName.PLANNING)
        callback = threading.Thread(target=pool._on_future_done, args=(handle, future))

        try:
            callback.start()
            callback.join(timeout=1)
            still_queued = completion_q.get_nowait()
        finally:
            if callback.is_alive():
                completion_q.get_nowait()
                callback.join(timeout=1)
            pool.shutdown(mark_interrupted=False)

        assert not callback.is_alive()
        assert still_queued is occupied
        assert saturation.is_set()
        assert wakeup.is_set()
        assert not shutdown_event.is_set()

    def test_submit_agent_job_propagates_prompt_dir_override_to_worker_thread(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Worker-thread prompt builders must see the CLI-selected prompt overlay."""
        template = tmp_path / "planning" / "plan.j2"
        template.parent.mkdir()
        template.write_text("WORKER {{ issue_number }}\n", encoding="utf-8")

        parser = build_automation_parser("test parser")
        try:
            parser.parse_args(["--prompt-dir", str(tmp_path)])
            seen: dict[str, str] = {}

            def prompt_builder() -> str:
                return PromptCatalog.current().render("planning/plan.j2", issue_number=7)

            job = _agent_job(prompt_builder=prompt_builder)

            def fake_invoke_claude_with_session(*args: object, **kwargs: object) -> tuple[str, str]:
                del args
                seen["prompt"] = str(kwargs["prompt"])
                return ("ok", "sid")

            with (
                patch(f"{_WP}.resolve_agent", return_value="claude"),
                patch(
                    f"{_WP}.claude_invoke.invoke_claude_with_session",
                    side_effect=fake_invoke_claude_with_session,
                ),
            ):
                pool.submit(job, StageName.IMPLEMENTATION)
                _, result = completion_q.get(timeout=10)

            assert result.ok is True
            assert WRITING_STANDARD_SENTINEL in seen["prompt"]
            assert seen["prompt"].endswith("WORKER 7\n")
        finally:
            PromptCatalog.clear_current()

    def test_submit_and_complete_agent_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Submit a Claude agent job and drain completion."""
        job = _agent_job()

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("Test output", "session-id")
            pool.submit(job, StageName.IMPLEMENTATION)
            handle, result = completion_q.get(timeout=10)

        assert handle.job is job
        assert handle.on_done_state == StageName.IMPLEMENTATION
        assert result.ok is True
        assert "Test output" in str(result.value)

    @pytest.mark.parametrize(
        "prompt",
        [
            pytest.param("ordinary prompt", id="ordinary"),
            pytest.param("sensitive-large-prompt:" + ("x" * 200_000), id="large"),
        ],
    )
    def test_claude_agent_job_requires_stdin_transport(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        prompt: str,
    ) -> None:
        """Pipeline Claude jobs forward prompts through stdin transport."""
        job = _agent_job(prompt_builder=lambda: prompt)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("Test output", "session-id"),
            ) as invoke,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert invoke.call_args.kwargs["prompt"] == prompt
        assert invoke.call_args.kwargs["input_via_stdin"] is True

    def test_compact_job_is_best_effort_and_returns_its_result(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A failed /compact never blocks the next review round."""
        job = CompactJob(
            repo="test/repo",
            issue=123,
            agent="claude",
            session_agent="implementer",
            model="claude-haiku-4-5",
            cwd=Path("/tmp"),
            timeout_s=60,
            pi_isolation_adapter="package:factory",
            pi_dir=Path("/private/pi-agent"),
        )
        with patch(f"{_WP}.compact_agent_session", return_value=False) as compact:
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value is False
        compact.assert_called_once_with(
            repo="test/repo",
            issue=123,
            provider="claude",
            session_agent="implementer",
            cwd=Path("/tmp"),
            timeout=60,
            model="claude-haiku-4-5",
            session_id=None,
            sandbox="read-only",
            execution_request=None,
            session_binding=None,
            disable_pi_automation=False,
            auth_status_timeout=10,
            pi_isolation_adapter="package:factory",
            pi_dir=Path("/private/pi-agent"),
        )

    def test_submit_and_complete_non_claude_agent_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Non-Claude agents dispatch through run_agent_session."""
        job = _agent_job(agent="codex")

        session_result = MagicMock(stdout="codex output", session_id="new-codex-session")
        with (
            patch(f"{_WP}.resolve_agent", return_value="codex") as mock_resolve,
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        mock_resolve.assert_called_once_with(
            "codex",
            cwd=job.cwd,
            disable_pi_automation=False,
            auth_status_timeout=10,
            pi_isolation_adapter=None,
            pi_dir=None,
            model_references=(job.model,),
        )
        mock_session.assert_called_once_with(
            agent="codex",
            prompt="test prompt",
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            sandbox="workspace-write",
            approval="never",
            process_tracker=subprocess_registry.track_process_group,
            execution_request=None,
            resume_binding=None,
            disable_pi_automation=False,
            pi_dir=None,
            isolate_codex_automation_profile=True,
        )
        assert result.ok is True
        assert result.value == "codex output"
        assert result.session_id == "new-codex-session"

    def test_codex_required_resume_rejects_an_unbound_raw_session(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A required Codex resume cannot silently discard its session context."""
        job = _agent_job(
            agent="codex",
            resume_session_id="saved-codex-session",
            execution_request=ExecutionRequest(
                AgentRole.IMPLEMENTER,
                AgentOperation.TEST_FIX,
                SessionLifecycle.RESUME_REQUIRED,
            ),
        )
        session_result = MagicMock(stdout="continued", session_id="saved-codex-session")

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.resume_agent_session", return_value=session_result) as resume,
            patch(f"{_WP}.run_agent_session", return_value=session_result) as run,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        resume.assert_not_called()
        run.assert_not_called()
        assert result.ok is False
        assert result.error == "agent_error: required agent session has no verified resume binding"

    def test_pi_default_fails_before_worker_agent_admission(
        self,
        pool: WorkerPool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A malformed Pi default must stop the worker before process-backed checks."""
        pi_dir = tmp_path / "pi-agent"
        pi_dir.mkdir()
        (pi_dir / "settings.json").write_text("{", encoding="utf-8")
        calls: list[str] = []
        monkeypatch.setattr(
            agent_runtime,
            "_require_pi_automation_admission",
            lambda *_args, **_kwargs: calls.append("admission"),
        )
        monkeypatch.setattr(
            agent_runtime,
            "_require_pi_isolation_adapter",
            lambda *_args, **_kwargs: calls.append("isolation"),
        )

        def record_authentication(*_args: object, **_kwargs: object) -> bool:
            calls.append("authentication")
            return True

        monkeypatch.setattr(
            agent_runtime,
            "is_agent_authenticated",
            record_authentication,
        )
        request = ExecutionRequest(
            AgentRole.PLANNER,
            AgentOperation.PLAN,
            SessionLifecycle.START_NEW,
        )
        job = _agent_job(
            agent="pi",
            model="",
            execution_request=request,
            cwd=tmp_path,
            pi_dir=pi_dir,
        )

        result = pool._run_agent(job)

        assert result.ok is False
        assert "Pi default model configuration" in (result.error or "")
        assert calls == []

    def test_non_codex_agent_job_resumes_a_saved_session(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A later direct-agent turn resumes, rather than starts afresh."""
        job = _agent_job(agent="opencode", resume_session_id="saved-opencode-session")
        session_result = MagicMock(stdout="continued", session_id="saved-opencode-session")

        with (
            patch(f"{_WP}.resolve_agent", return_value="opencode"),
            patch(f"{_WP}.resume_agent_session", return_value=session_result) as resume,
            patch(f"{_WP}.run_agent_session") as run,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)

        resume.assert_called_once_with(
            agent="opencode",
            session_id="saved-opencode-session",
            prompt="test prompt",
            cwd=job.cwd,
            timeout=job.timeout_s,
            model=job.model,
            sandbox="workspace-write",
            approval="never",
            process_tracker=subprocess_registry.track_process_group,
            execution_request=None,
            resume_binding=None,
            disable_pi_automation=False,
            pi_dir=None,
        )
        run.assert_not_called()
        assert result.ok is True
        assert result.value == "continued"
        assert result.session_id == "saved-opencode-session"

    def test_pi_agent_job_uses_its_binding_instead_of_a_raw_resume_id(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Worker dispatch preserves Pi's validated resume identity and request."""
        request = ExecutionRequest(
            AgentRole.PR_REVIEWER,
            AgentOperation.PR_REVIEW,
            SessionLifecycle.RESUME_REQUIRED,
        )
        binding = create_pi_binding(
            session_id="saved-pi-session",
            cwd=Path("/tmp"),
            role=AgentRole.PR_REVIEWER,
            model="opus-4-8",
        )
        job = _agent_job(
            agent="pi",
            execution_request=request,
            resume_binding=binding,
        )
        session_result = AgentRunResult(
            stdout="continued",
            stderr="",
            session_id=binding.session_id,
            session_binding=binding,
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="pi"),
            patch(f"{_WP}.resume_agent_session", return_value=session_result) as resume,
            patch(f"{_WP}.run_agent_session") as run,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert resume.call_args.kwargs["session_id"] == binding.session_id
        assert resume.call_args.kwargs["execution_request"] == request
        assert resume.call_args.kwargs["resume_binding"] == binding
        run.assert_not_called()

    def test_resumed_read_only_agent_job_preserves_its_sandbox(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Resuming review work must not widen the direct agent's tools."""
        job = _agent_job(
            agent="pi",
            sandbox="read-only",
            resume_session_id="saved-pi-session",
        )
        session_result = MagicMock(stdout="continued", session_id="saved-pi-session")

        with (
            patch(f"{_WP}.resolve_agent", return_value="pi"),
            patch(f"{_WP}.resume_agent_session", return_value=session_result) as resume,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert resume.call_args.kwargs["sandbox"] == "read-only"
        assert resume.call_args.kwargs["approval"] == "never"

    def test_read_only_agent_job_propagates_its_sandbox_to_codex(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Read-only jobs must not be silently widened to workspace-write."""
        job = _agent_job(agent="codex", sandbox="read-only")
        session_result = MagicMock(stdout="review")

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.run_agent_session", return_value=session_result) as mock_session,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert mock_session.call_args.kwargs["sandbox"] == "read-only"

    def test_read_only_agent_job_scopes_claude_to_read_tools(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A read-only job keeps Claude's historic restricted tool scope."""
        job = _agent_job(sandbox="read-only")
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("GO", "s"),
            ) as invoke,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert invoke.call_args.kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert invoke.call_args.kwargs["permission_mode"] == "dontAsk"

    def test_read_only_agent_job_honors_its_explicit_skill_scope(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """The PR-review worker can invoke its declared read-only skill."""
        allowed_tools = "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"
        job = _agent_job(sandbox="read-only", allowed_tools=allowed_tools)
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("GO", "s"),
            ) as invoke,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _handle, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert invoke.call_args.kwargs["allowed_tools"] == allowed_tools
        assert invoke.call_args.kwargs["permission_mode"] == "dontAsk"

    def test_submit_and_complete_build_test_job(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Submit a build/test job."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("echo", "hello"),
            timeout_s=60,
        )

        pool.submit(job, StageName.PR_REVIEW)
        handle, result = completion_q.get(timeout=10)

        assert handle.job is job
        assert result.ok is True
        assert "hello" in result.stdout_tail

    def test_worker_claim_logs_and_result_carries_worker_id(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A worker records its identity when it starts executing a submitted job."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=tmp_path,
            argv=("pytest", "-q"),
            timeout_s=60,
            descr="unit gate",
        )
        completed = subprocess.CompletedProcess(job.argv, 0, stdout="", stderr="")
        caplog.set_level(logging.INFO, logger=_WP)

        with patch(f"{_WP}.subprocess.run", return_value=completed):
            pool.submit(
                job,
                StageName.PR_REVIEW,
                claim_key="test/repo#123",
                claim_stage="ci",
            )
            _handle, result = completion_q.get(timeout=10)

        worker_id = getattr(result, "worker_id", "")
        assert worker_id
        assert any(
            "worker_claim" in record.message
            and worker_id in record.message
            and "item=test/repo#123" in record.message
            and "stage=ci" in record.message
            for record in caplog.records
        )

    def test_distinct_workers_claim_concurrent_queue_entries(
        self,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Concurrent entries expose distinct worker IDs in claim logs and results."""
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        jobs = [
            BuildTestJob(
                repo="test/repo",
                cwd=tmp_path,
                argv=("pytest", "-q", f"case-{idx}"),
                timeout_s=60,
                descr=f"unit gate {idx}",
            )
            for idx in range(2)
        ]
        barrier = threading.Barrier(2, timeout=5)

        def complete_after_both_workers_enter(
            argv: tuple[str, ...],
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            barrier.wait()
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        caplog.set_level(logging.INFO, logger=_WP)

        try:
            with patch(f"{_WP}.subprocess.run", side_effect=complete_after_both_workers_enter):
                pool.submit(
                    jobs[0],
                    StageName.PR_REVIEW,
                    claim_key="test/repo#123",
                    claim_stage="ci",
                )
                pool.submit(
                    jobs[1],
                    StageName.PR_REVIEW,
                    claim_key="test/repo!456",
                    claim_stage="pr_review",
                )
                results = [completion_q.get(timeout=10)[1] for _ in jobs]
        finally:
            pool.shutdown()

        worker_ids = {result.worker_id for result in results}
        assert len(worker_ids) == 2
        claim_messages = [
            record.message for record in caplog.records if "worker_claim" in record.message
        ]
        assert any(
            worker_id in message and "item=test/repo#123" in message and "stage=ci" in message
            for worker_id in worker_ids
            for message in claim_messages
        )
        assert any(
            worker_id in message
            and "item=test/repo!456" in message
            and "stage=pr_review" in message
            for worker_id in worker_ids
            for message in claim_messages
        )

    def test_build_test_nonzero_rc_is_not_ok(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Build/test job with nonzero rc returns ok=False."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("false",),
            timeout_s=60,
        )

        pool.submit(job, StageName.PR_REVIEW)
        _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "rc=1" in result.error

    def test_build_test_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Build/test job hitting its timeout returns an error result."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("sleep", "60"),
            timeout_s=1,
        )

        with patch(
            f"{_WP}.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["sleep", "60"], timeout=1),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "timeout"

    def test_pytest_failure_classification_requires_terminal_summary(self) -> None:
        """A bootstrap error mentioning failures is never implementation work."""
        assert _confirmed_pytest_failure(
            1,
            "========================= 1 failed, 5 passed in 0.42s =========================\n",
            "",
        )
        assert _confirmed_pytest_failure(
            1,
            "=========================== short test summary info ============================\n"
            "FAILED tests/unit/test_example.py::test_example - assert False\n"
            "1 failed, 6199 passed, 26 skipped in 121.30s\n",
            "",
        )
        assert _confirmed_pytest_failure(
            1,
            "=========================== short test summary info ============================\n"
            "FAILED tests/unit/test_example.py::test_example - assert False\n"
            "1 failed, 6199 passed, 26 skipped, 64 deselected, 83 warnings in 100.48s "
            "(0:01:40)\n",
            "",
        )
        assert not _confirmed_pytest_failure(
            1,
            "uv bootstrap error: 1 failed to prepare environment",
            "",
        )

    def test_fixed_lint_and_type_failures_are_actionable_validation_work(self) -> None:
        """Known tool diagnostics reach remediation; bootstrap errors do not."""
        assert (
            _host_validation_failure_kind(
                ("uv", "run", "ruff", "check", "hephaestus/"),
                1,
                "Found 1 error.\n",
                "",
            )
            == "validation"
        )
        assert (
            _host_validation_failure_kind(
                ("uv", "run", "mypy", "hephaestus/"),
                1,
                "Found 2 errors in 1 file (checked 3 source files)\n",
                "",
            )
            == "validation"
        )
        assert (
            _host_validation_failure_kind(
                ("uv", "run", "ruff", "format", "--check", "hephaestus/"),
                1,
                "unformatted: File would be reformatted\n3 files would be reformatted\n",
                "",
            )
            == "validation"
        )
        assert (
            _host_validation_failure_kind(
                ("uv", "run", "ruff", "check", "hephaestus/"),
                2,
                "",
                "uv failed to prepare the environment",
            )
            == "runner"
        )

    def test_bounded_host_command_disconnects_stdin_and_unregisters_process_group(
        self, tmp_path: Path
    ) -> None:
        """Untrusted validation code gets no inherited input or leaked group."""
        source = tmp_path / "source"
        scratch = tmp_path / "scratch"
        source.mkdir()
        scratch.mkdir()

        result = _run_bounded_host_command(
            (sys.executable, "-c", "import sys; raise SystemExit(sys.stdin.read() != '')"),
            validation_argv=("uv", "run", "pytest", "tests/unit"),
            source=source,
            scratch=scratch,
            environment=dict(os.environ),
            timeout_s=5,
            shutdown=threading.Event(),
        )

        assert result.ok is True
        assert subprocess_registry.live_count() == 0

    def test_bounded_host_command_stops_immediately_when_pool_is_interrupted(
        self, tmp_path: Path
    ) -> None:
        """A stopping loop terminates a host child rather than waiting for timeout."""
        source = tmp_path / "source"
        scratch = tmp_path / "scratch"
        source.mkdir()
        scratch.mkdir()
        shutdown = threading.Event()
        shutdown.set()

        result = _run_bounded_host_command(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            validation_argv=("uv", "run", "pytest", "tests/unit"),
            source=source,
            scratch=scratch,
            environment=dict(os.environ),
            timeout_s=60,
            shutdown=shutdown,
        )

        assert result.interrupted is True
        assert result.error == "interrupted"
        assert subprocess_registry.live_count() == 0

    def test_immutable_build_test_runs_from_disposable_head_snapshot(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Host verification never lets PR code mutate the reviewer checkout."""
        checkout = tmp_path / "checkout"
        subprocess.run(
            ["git", "init", "--initial-branch", "main", str(checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
            subprocess.run(
                ["git", "config", key, value],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            )
        (checkout / "tracked.txt").write_text("original\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "fixture"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        program = (
            "from pathlib import Path; import subprocess; "
            "assert Path('.git').is_file(); "
            "assert Path('build').is_symlink(); "
            "assert Path('pi-smoke-logs').is_dir(); "
            "assert not Path('pi-smoke-logs').is_symlink(); "
            "resolved = subprocess.check_output(('git', 'rev-parse', 'HEAD'), text=True).strip(); "
            f"assert resolved == {head!r}"
        )
        job = BuildTestJob(
            repo="test/repo",
            cwd=checkout,
            argv=(
                sys.executable,
                "-c",
                program,
            ),
            timeout_s=60,
            expected_head_sha=head,
            immutable_source=True,
        )

        # The host OS boundary has its own command-construction tests.  Keep
        # this fixture focused on the archive and Git-metadata boundaries: the
        # child sees a disposable source snapshot and matching sealed Git data,
        # never the reviewer checkout.
        def disposable_scratch(root: Path) -> object:
            scratch = root / "scratch"
            scratch.mkdir()
            return nullcontext(scratch)

        def disposable_pi_smoke_logs(root: Path, source: Path) -> object:
            logs = source / "pi-smoke-logs"
            logs.mkdir()
            return nullcontext(logs)

        with (
            patch(f"{_WP}.sys.platform", "darwin"),
            patch(
                f"{_WP}._verifier_owned_runtime_environment",
                return_value=Path(sys.prefix),
            ),
            patch(
                f"{_WP}._host_verification_command",
                side_effect=lambda **kwargs: kwargs["argv"],
            ),
            patch(f"{_WP}._quota_backed_scratch", side_effect=disposable_scratch),
            patch(
                f"{_WP}._quota_backed_pi_smoke_logs",
                side_effect=disposable_pi_smoke_logs,
            ),
        ):
            result = pool._run_build_test(job)

        assert result.ok is True
        assert result.value == {
            "head_sha": head,
            "immutable_source": True,
            "failure_kind": "none",
            "platform": "darwin",
            "status": "passed",
        }
        assert (checkout / "tracked.txt").read_text(encoding="utf-8") == "original\n"

    def test_immutable_build_test_skips_unsupported_platform_before_execution(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """An unsupported host records a bound skip without executing PR code."""
        job = BuildTestJob(
            repo="test/repo",
            cwd=tmp_path,
            argv=(sys.executable, "-c", "raise SystemExit(0)"),
            timeout_s=60,
            expected_head_sha="a" * 40,
            immutable_source=True,
        )

        archive = MagicMock(return_value=(b"", ""))
        with (
            patch(f"{_WP}._checkout_matches_immutable_head", return_value=None),
            patch(f"{_WP}._trusted_executable", return_value=sys.executable),
            patch(
                f"{_WP}._verifier_owned_runtime_environment",
                return_value=Path(sys.prefix),
            ),
            patch(f"{_WP}._bounded_git_archive", archive),
            patch(f"{_WP}._extract_immutable_archive"),
            patch(f"{_WP}._prepare_immutable_git_metadata", return_value=tmp_path / "metadata.git"),
            patch(f"{_WP}._quota_backed_scratch", side_effect=nullcontext),
            patch(
                f"{_WP}._quota_backed_pi_smoke_logs",
                side_effect=lambda root, source: nullcontext(source / "pi-smoke-logs"),
            ),
            patch(f"{_WP}.sys.platform", "linux"),
        ):
            result = pool._run_build_test(job)

        assert result.ok is False
        assert result.error == "unsupported_host_verification_boundary"
        assert result.value == {
            "failure_kind": "runner",
            "head_sha": "a" * 40,
            "immutable_source": False,
            "platform": "linux",
            "status": "skipped",
        }
        archive.assert_not_called()

    def test_host_verification_profile_keeps_source_outside_writable_root(
        self, tmp_path: Path
    ) -> None:
        """Only the separate scratch tree is writable to PR-controlled code."""
        source = tmp_path / "source"
        scratch = tmp_path / "scratch"
        runtime = tmp_path / "runtime"
        pi_smoke_logs = source / "pi-smoke-logs"
        profile = _host_verification_profile(
            source=source,
            scratch=scratch,
            runtime_environment=runtime,
            git_metadata=tmp_path / "metadata.git",
            pi_smoke_logs=pi_smoke_logs,
            executable=Path("/usr/bin/uv"),
        )

        source_entry = f'(subpath "{source.resolve()}")'
        scratch_entry = f'(subpath "{scratch.resolve()}")'
        pi_smoke_logs_entry = f'(subpath "{pi_smoke_logs.resolve()}")'
        assert '(import "system.sb")' in profile
        assert "(deny network*)" in profile
        assert "(allow signal (target same-sandbox))" in profile
        assert "(allow signal)" not in profile
        assert '(allow ipc-posix-sem (ipc-posix-name-prefix "/mp-"))' in profile
        assert f'(subpath "{Path("/bin").resolve()}")' in profile
        assert f'  (literal "{Path("/tmp").resolve()}")' not in profile
        assert f'(allow file-read-metadata (literal "{Path("/tmp").resolve()}"))' in profile
        assert f'(allow file-read-metadata (path-ancestors "{source.resolve()}"))' in profile
        assert source_entry in profile
        assert f"(allow file-write* {source_entry})" not in profile
        assert f"(allow file-write* {scratch_entry})" in profile
        assert f"(allow file-write* {pi_smoke_logs_entry})" in profile

    def test_hdiutil_blank_image_argv_uses_no_srcfolder_only_format(self, tmp_path: Path) -> None:
        """The quota image uses the valid blank-HFS+ form accepted by macOS."""
        argv = _hdiutil_create_argv(tmp_path / "scratch.dmg")

        assert argv[:6] == ("/usr/bin/hdiutil", "create", "-size", "512m", "-fs", "HFS+")
        assert "-format" not in argv

    def test_host_verification_allows_coverage_database_within_volume_quota(
        self, tmp_path: Path
    ) -> None:
        """The per-file limit leaves headroom for coverage's SQLite database."""
        source = tmp_path / "source"
        scratch = tmp_path / "scratch"
        runtime = tmp_path / "runtime"
        metadata = tmp_path / "metadata.git"
        pi_smoke_logs = source / "pi-smoke-logs"
        for directory in (source, scratch, runtime, metadata, pi_smoke_logs):
            directory.mkdir(parents=True, exist_ok=True)

        with (
            patch(f"{_WP}.sys.platform", "darwin"),
            patch.object(Path, "is_file", return_value=True),
            patch(f"{_WP}.os.access", return_value=True),
        ):
            command = _host_verification_command(
                argv=(sys.executable, "-m", "pytest"),
                source=source,
                scratch=scratch,
                runtime_environment=runtime,
                git_metadata=metadata,
                pi_smoke_logs=pi_smoke_logs,
            )

        assert "limit -f 131072" in command[2]

    def test_quota_volume_retries_a_timed_out_detach(self, tmp_path: Path) -> None:
        """A transient forced-detach timeout cannot leak a verifier volume."""
        mountpoint = tmp_path / "scratch"
        mountpoint.mkdir()
        completed: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess([], 0)

        with (
            patch(f"{_WP}.sys.platform", "darwin"),
            patch.object(Path, "is_file", return_value=True),
            patch(f"{_WP}.os.access", return_value=True),
            patch(
                f"{_WP}.subprocess.run",
                side_effect=(
                    completed,
                    completed,
                    subprocess.TimeoutExpired(cmd="hdiutil detach", timeout=15),
                    completed,
                ),
            ) as run,
        ):
            with _quota_backed_volume(tmp_path, "scratch.dmg", mountpoint) as mounted:
                assert mounted == mountpoint

        assert run.call_count == 4

    def test_quota_volume_fails_closed_after_two_detach_failures(self, tmp_path: Path) -> None:
        """Unconfirmed cleanup is an explicit host-boundary failure."""
        mountpoint = tmp_path / "scratch"
        mountpoint.mkdir()
        completed: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess([], 0)
        failed_detach: subprocess.CompletedProcess[Any] = subprocess.CompletedProcess([], 1)

        with (
            patch(f"{_WP}.sys.platform", "darwin"),
            patch.object(Path, "is_file", return_value=True),
            patch(f"{_WP}.os.access", return_value=True),
            patch(
                f"{_WP}.subprocess.run",
                side_effect=(completed, completed, failed_detach, failed_detach),
            ) as run,
            pytest.raises(RuntimeError, match="host_verification_quota_cleanup_failed"),
        ):
            with _quota_backed_volume(tmp_path, "scratch.dmg", mountpoint):
                pass

        assert run.call_count == 4

    def test_verifier_runtime_rejects_an_incomplete_cache_entry(self, tmp_path: Path) -> None:
        """A pre-seal runtime cache cannot be reused after an interrupted copy."""
        checkout = tmp_path / "checkout"
        runtime = checkout / ".venv"
        runtime.mkdir(parents=True)
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        cache_temp = tmp_path / "cache-temp"
        incomplete = cache_temp / "hephaestus-host-validation-runtime" / "fixture-runtime"
        incomplete.mkdir(parents=True)

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
            patch(f"{_WP}._host_runtime_fingerprint", return_value="fixture-runtime"),
            pytest.raises(RuntimeError, match="host_verification_runtime_cache_unsafe"),
        ):
            _verifier_owned_runtime_environment(checkout)

    def test_verifier_runtime_snapshots_external_worker_environment(self, tmp_path: Path) -> None:
        """The verifier never exposes a live worker environment to the sandbox."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        runtime = tmp_path / "worker-runtime"
        launcher = runtime / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("host interpreter\n", encoding="utf-8")
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)

        assert sealed != runtime
        assert sealed.is_relative_to(cache_temp / "hephaestus-host-validation-runtime")
        assert (sealed / "bin" / "python").read_text(encoding="utf-8") == "host interpreter\n"
        assert not ((sealed / "bin" / "python").stat().st_mode & 0o222)

    def test_verifier_runtime_does_not_reuse_a_different_dependency_set(
        self, tmp_path: Path
    ) -> None:
        """A changed installed package set receives a fresh sealed runtime."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        cache_temp = tmp_path / "cache-temp"

        def make_runtime(name: str, record: str) -> Path:
            runtime = tmp_path / name
            (runtime / "bin").mkdir(parents=True)
            (runtime / "bin" / "python").write_text("host interpreter\n", encoding="utf-8")
            (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
            site_packages = runtime / "lib" / "python3.13" / "site-packages"
            dist_info = site_packages / "demo-1.0.dist-info"
            dist_info.mkdir(parents=True)
            (dist_info / "RECORD").write_text(record, encoding="utf-8")
            (site_packages / "demo.py").write_text("value = 1\n", encoding="utf-8")
            return runtime

        first_runtime = make_runtime("runtime-first", "demo.py,sha256=first,1\n")
        second_runtime = make_runtime("runtime-second", "demo.py,sha256=second,2\n")

        with (
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
            patch(f"{_WP}.sys.prefix", str(first_runtime)),
        ):
            first = _verifier_owned_runtime_environment(checkout)
        with (
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
            patch(f"{_WP}.sys.prefix", str(second_runtime)),
        ):
            second = _verifier_owned_runtime_environment(checkout)

        assert second != first
        assert (
            second / "lib" / "python3.13" / "site-packages" / "demo-1.0.dist-info" / "RECORD"
        ).read_text(encoding="utf-8") == "demo.py,sha256=second,2\n"

    def test_verifier_runtime_rebuilds_cache_missing_recorded_file(self, tmp_path: Path) -> None:
        """A completion marker cannot mask a missing installed runtime file."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        runtime = tmp_path / "runtime"
        site_packages = runtime / "lib" / "python3.13" / "site-packages"
        dist_info = site_packages / "demo-1.0.dist-info"
        (runtime / "bin").mkdir(parents=True)
        dist_info.mkdir(parents=True)
        (runtime / "bin" / "python").write_text("host interpreter\n", encoding="utf-8")
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (site_packages / "demo.py").write_text("value = 1\n", encoding="utf-8")
        (dist_info / "RECORD").write_text("demo.py,sha256=fixture,10\n", encoding="utf-8")
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)
            sealed_site_packages = sealed / "lib" / "python3.13" / "site-packages"
            sealed_site_packages.chmod(sealed_site_packages.stat().st_mode | 0o200)
            (sealed_site_packages / "demo.py").unlink()
            rebuilt = _verifier_owned_runtime_environment(checkout)
            with patch(f"{_WP}.shutil.copytree", side_effect=AssertionError("unexpected copy")):
                reused = _verifier_owned_runtime_environment(checkout)

        assert rebuilt == sealed
        assert reused == sealed
        assert (rebuilt / "lib" / "python3.13" / "site-packages" / "demo.py").read_text(
            encoding="utf-8"
        ) == "value = 1\n"

    def test_verifier_runtime_rebuilds_cache_missing_record_manifest(self, tmp_path: Path) -> None:
        """A missing RECORD file cannot make a sealed cache self-validate."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        runtime = tmp_path / "runtime"
        site_packages = runtime / "lib" / "python3.13" / "site-packages"
        dist_info = site_packages / "demo-1.0.dist-info"
        (runtime / "bin").mkdir(parents=True)
        dist_info.mkdir(parents=True)
        (runtime / "bin" / "python").write_text("host interpreter\n", encoding="utf-8")
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (site_packages / "demo.py").write_text("value = 1\n", encoding="utf-8")
        (dist_info / "RECORD").write_text(
            "demo.py,sha256=fixture,10\ndemo-1.0.dist-info/RECORD,,\n",
            encoding="utf-8",
        )
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)
            sealed_dist_info = (
                sealed / "lib" / "python3.13" / "site-packages" / "demo-1.0.dist-info"
            )
            sealed_dist_info.chmod(sealed_dist_info.stat().st_mode | 0o200)
            (sealed_dist_info / "RECORD").unlink()
            rebuilt = _verifier_owned_runtime_environment(checkout)

        assert rebuilt == sealed
        assert (
            rebuilt / "lib" / "python3.13" / "site-packages" / "demo-1.0.dist-info" / "RECORD"
        ).read_text(encoding="utf-8") == (
            "demo.py,sha256=fixture,10\ndemo-1.0.dist-info/RECORD,,\n"
        )

    def test_verifier_runtime_reuses_intact_manifest_cache(self, tmp_path: Path) -> None:
        """Integrity checks do not recopy an intact sealed environment."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        runtime = tmp_path / "runtime"
        site_packages = runtime / "lib" / "python3.13" / "site-packages"
        dist_info = site_packages / "demo-1.0.dist-info"
        (runtime / "bin").mkdir(parents=True)
        dist_info.mkdir(parents=True)
        (runtime / "bin" / "python").write_text("host interpreter\n", encoding="utf-8")
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (site_packages / "demo.py").write_text("value = 1\n", encoding="utf-8")
        (dist_info / "RECORD").write_text("demo.py,sha256=fixture,10\n", encoding="utf-8")
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)
            with patch(f"{_WP}.shutil.copytree", side_effect=AssertionError("unexpected copy")):
                reused = _verifier_owned_runtime_environment(checkout)

        assert reused == sealed

    def test_verifier_runtime_dereferences_the_python_launcher(self, tmp_path: Path) -> None:
        """The sealed copy does not retain a launcher back into its source runtime."""
        checkout = tmp_path / "checkout"
        runtime = checkout / ".venv"
        launcher = runtime / "bin" / "python"
        external_launcher = tmp_path / "base" / "python"
        launcher.parent.mkdir(parents=True)
        external_launcher.parent.mkdir()
        external_launcher.write_text("host interpreter\n", encoding="utf-8")
        os.symlink(external_launcher, launcher)
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (runtime / "bin" / "mypy").write_text(
            f"#!{runtime.resolve()}/bin/python\nentry point\n", encoding="utf-8"
        )
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)

        copied_launcher = sealed / "bin" / "python"
        assert not copied_launcher.is_symlink()
        assert copied_launcher.read_text(encoding="utf-8") == "host interpreter\n"
        assert (
            (sealed / "bin" / "mypy")
            .read_text(encoding="utf-8")
            .startswith(f"#!{sealed.resolve()}/bin/python\n")
        )

    def test_verifier_runtime_rewrites_uv_long_path_shell_launcher(self, tmp_path: Path) -> None:
        """A uv shell trampoline executes only the sealed runtime interpreter."""
        checkout = tmp_path / "checkout"
        runtime = checkout / ("long-runtime-path-" + "x" * 120) / ".venv"
        launcher = runtime / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("host interpreter\n", encoding="utf-8")
        (runtime / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        source_python = runtime.resolve() / "bin" / "python"
        (runtime / "bin" / "mypy").write_text(
            "#!/bin/sh\n"
            f"'''exec' '{source_python}' \"$0\" \"$@\"\n"
            "' '''\n"
            "from mypy.__main__ import console_entry\n",
            encoding="utf-8",
        )
        cache_temp = tmp_path / "cache-temp"

        with (
            patch(f"{_WP}.sys.prefix", str(runtime)),
            patch(f"{_WP}.tempfile.gettempdir", return_value=str(cache_temp)),
        ):
            sealed = _verifier_owned_runtime_environment(checkout)

        copied = (sealed / "bin" / "mypy").read_text(encoding="utf-8")
        assert str(source_python) not in copied
        assert f"'''exec' '{sealed.resolve() / 'bin' / 'python'}'" in copied

    def test_host_verification_environment_keeps_tool_output_in_scratch(
        self, tmp_path: Path
    ) -> None:
        """UV, Ruff, pytest coverage, and bytecode write only to scratch."""
        scratch = tmp_path / "scratch"

        environment = _host_verification_env(scratch, "/usr/bin/uv", tmp_path / "runtime")

        for key in (
            "UV_CACHE_DIR",
            "RUFF_CACHE_DIR",
            "COVERAGE_FILE",
            "PYTHONPYCACHEPREFIX",
        ):
            assert Path(environment[key]).is_relative_to(scratch.resolve())
        assert environment["PYTEST_ADDOPTS"] == "-p no:cacheprovider"

    def test_host_output_aliases_keep_coverage_xml_in_scratch(self, tmp_path: Path) -> None:
        """The full coverage receipt cannot write into the immutable source tree."""
        source = tmp_path / "source"
        scratch = tmp_path / "scratch"
        source.mkdir()
        scratch.mkdir()

        _prepare_host_output_aliases(source, scratch)

        assert (source / "coverage.xml").is_symlink()
        assert (source / "coverage.xml").read_text(encoding="utf-8") == ""
        (source / "coverage.xml").write_text("<coverage />", encoding="utf-8")
        assert (scratch / "coverage.xml").read_text(encoding="utf-8") == "<coverage />"


class TestAgentErrorHandling:
    """Tests for agent-job error handling paths."""

    def test_agent_breaker_is_shared_across_models(
        self,
        pool: WorkerPool,
    ) -> None:
        """Failures for one model open the runtime breaker for every model."""
        get_circuit_breaker("agent:claude", failure_threshold=2)
        jobs = [_agent_job(model=model) for model in ("opus", "sonnet")]

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=RuntimeError("runtime unavailable"),
            ) as invoke,
        ):
            first = pool._run_agent(jobs[0])
            second = pool._run_agent(jobs[0])
            blocked = pool._run_agent(jobs[1])

        assert first.error == "RuntimeError: runtime unavailable"
        assert second.error == "RuntimeError: runtime unavailable"
        assert blocked.error == "circuit_open"
        assert invoke.call_count == 2

    def test_circuit_breaker_open_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent job with circuit open returns error result."""
        job = _agent_job(model="model-cb-open", prompt_builder=lambda: "prompt")

        def failing_invoke(*args: object, **kwargs: object) -> object:
            raise CircuitBreakerOpenError(name="test_breaker", time_until_recovery=10.0)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=failing_invoke,
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "circuit_open"

    def test_agent_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent invocation timeout maps to error='timeout' (not retried)."""
        job = _agent_job(model="model-agent-timeout")

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=subprocess.TimeoutExpired(cmd=["claude"], timeout=60),
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=30)

        assert result.ok is False
        assert result.error == "timeout"

    def test_agent_called_process_error_returns_rc_and_tails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Agent CalledProcessError maps to rc=<n> with stdout/stderr tails."""
        job = _agent_job(model="model-agent-cpe")
        exc = subprocess.CalledProcessError(
            returncode=2,
            cmd=["claude"],
            output="partial stdout",
            stderr="nonretryable failure detail",
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=exc,
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=30)

        assert result.ok is False
        assert result.error == "rc=2"
        assert "partial stdout" in result.stdout_tail
        assert "nonretryable failure detail" in result.stderr_tail

    def test_resume_process_error_reports_session_lost(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A provider resume rejection is terminal instead of a fresh review."""
        job = _agent_job(model="model-agent-cpe", resume_session_id="opaque-session")
        exc = subprocess.CalledProcessError(
            returncode=2,
            cmd=["claude"],
            output="",
            stderr="No conversation found for session opaque-session",
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=exc,
            ),
        ):
            pool.submit(job, StageName.PLAN_REVIEW)
            _, result = completion_q.get(timeout=30)

        assert result.ok is False
        assert result.error == "review-session-lost"
        assert result.session_lost is True

    def test_codex_event_failure_is_explicit_agent_error(self, pool: WorkerPool) -> None:
        """Structured Codex failures cross the worker boundary as agent errors."""
        job = _agent_job(agent="codex")

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(
                f"{_WP}.run_agent_session",
                side_effect=AgentExecutionError(
                    "codex_nested_sandbox_unsupported: run the outer loop "
                    "outside the enclosing API sandbox"
                ),
            ),
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("agent_error: codex_nested_sandbox_unsupported")
        assert "outside the enclosing API sandbox" in result.error

    def test_pr_review_prompt_limit_stops_before_provider_call(self, pool: WorkerPool) -> None:
        """A deterministic prompt limit error cannot reach the provider."""

        def oversized_prompt() -> str:
            raise PrReviewPromptSizeError(
                "pr_review_prompt_limit_exceeded: required prompt content exceeds 350000 characters"
            )

        job = _agent_job(agent="codex", prompt_builder=oversized_prompt)

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(f"{_WP}.run_agent_session") as mock_agent,
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == (
            "PrReviewPromptSizeError: pr_review_prompt_limit_exceeded: required prompt content "
            "exceeds 350000 characters"
        )
        mock_agent.assert_not_called()

    def test_codex_skills_budget_notice_does_not_open_agent_breaker(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """An informational Codex notice remains successful across the worker boundary."""
        job = _agent_job(agent="codex")
        breaker = get_circuit_breaker("agent:codex")
        notice = (
            "Skill descriptions were shortened to fit the skills context budget. "
            "Codex can still see every skill, but some descriptions are shorter. "
            "Disable unused skills or plugins to leave more room for the rest."
        )

        def fake_popen(cmd: list[str], **_kwargs: Any) -> MagicMock:
            stdout = "\n".join(
                [
                    json.dumps({"type": "thread.started", "thread_id": "codex-session"}),
                    json.dumps(
                        {
                            "type": "item.completed",
                            "item": {
                                "id": "item_1",
                                "type": "error",
                                "message": notice,
                            },
                        }
                    ),
                    json.dumps({"type": "turn.completed", "usage": {}}),
                ]
            )
            output_path = Path(cmd[cmd.index("--output-last-message") + 1])
            output_path.write_text(
                "Completed despite the informational notice.",
                encoding="utf-8",
            )
            process = MagicMock()
            process.pid = 2468
            process.returncode = 0
            process.communicate.return_value = (stdout, "")
            process.poll.return_value = 0
            return process

        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch(
                "hephaestus.agents.runtime.codex_approval_args",
                return_value=[],
            ),
            patch(
                "hephaestus.agents.runtime._codex_extra_writable_dirs",
                return_value=[],
            ),
            patch(
                "hephaestus.agents.runtime.shutil.which",
                return_value="/usr/bin/codex",
            ),
            patch(
                "hephaestus.agents.runtime._codex_automation_profile",
                return_value=nullcontext({"CODEX_HOME": str(tmp_path), "PATH": os.environ["PATH"]}),
            ),
            patch(
                "hephaestus.agents.runtime.macos_isolated_command",
                side_effect=lambda argv, **_kwargs: list(argv),
            ),
            patch(
                "hephaestus.agents.runtime.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                f"{_WP}.subprocess_registry.track_process_group",
                side_effect=lambda _pid: nullcontext(),
            ),
        ):
            results = [pool._run_agent(job) for _ in range(breaker.failure_threshold + 1)]

        assert all(result.ok for result in results)
        assert all(result.error is None for result in results)
        assert breaker.snapshot()["state"] == "closed"
        assert breaker.snapshot()["failure_count"] == 0

    def test_generic_exception_converted_to_error_result(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An unexpected exception inside the job is bounded by the shared cap."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)

        def exploding_builder() -> str:
            raise RuntimeError("prompt builder exploded " + ("x" * 200))

        job = _agent_job(model="model-generic-exc", prompt_builder=exploding_builder)

        with patch(f"{_WP}.resolve_agent", return_value="claude"):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("RuntimeError: ")
        assert len(result.error) == small_err_max

    def test_run_agent_classifies_resolve_agent_exception(
        self,
        pool: WorkerPool,
    ) -> None:
        """resolve_agent failures are classified inside _run_agent."""
        job = _agent_job(model="model-resolve-generic", agent="bad-agent")

        with patch(f"{_WP}.resolve_agent", side_effect=ValueError("bad agent")):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "ValueError: bad agent"

    def test_run_agent_classifies_prompt_builder_exception(self, pool: WorkerPool) -> None:
        """Prompt builder failures are classified inside _run_agent."""

        def missing_prompt() -> str:
            raise KeyError("prompt-template")

        job = _agent_job(model="model-prompt-generic", prompt_builder=missing_prompt)

        with patch(f"{_WP}.resolve_agent", return_value="claude"):
            result = pool._run_agent(job)

        assert result.ok is False
        assert "KeyError" in (result.error or "")
        assert "prompt-template" in (result.error or "")

    def test_run_converts_escaping_exception_to_bounded_error(
        self,
        pool: WorkerPool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Exceptions escaping _run_agent are still capped in _run."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)
        job = _agent_job(model="model-run-generic", prompt_builder=lambda: "prompt")

        with patch.object(pool, "_run_agent", side_effect=RuntimeError("z" * 200)):
            result = pool._run(job)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("RuntimeError: ")
        assert len(result.error) == small_err_max

    def test_run_agent_classifies_resilient_call_exception(self, pool: WorkerPool) -> None:
        """Unexpected resilience-wrapper failures are classified inside _run_agent."""
        job = _agent_job(model="model-resilient-generic", prompt_builder=lambda: "prompt")

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.resilient_call", side_effect=OSError("retry wrapper failed")),
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "OSError: retry wrapper failed"

    def test_run_agent_does_not_retry_transient_error_after_shutdown(
        self, pool: WorkerPool, shutdown_event: threading.Event
    ) -> None:
        """Shutdown suppresses retrying an interrupted agent session."""
        job = _agent_job(model="model-shutdown-no-retry", prompt_builder=lambda: "prompt")
        shutdown_event.set()

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                side_effect=OSError("connection reset"),
            ) as invoke,
            patch("hephaestus.utils.retry.time.sleep") as sleep,
        ):
            result = pool._run_agent(job)

        assert result.ok is False
        assert result.error == "OSError: connection reset"
        assert invoke.call_count == 1
        sleep.assert_not_called()

    def test_unknown_job_type_returns_error_result(self, pool: WorkerPool) -> None:
        """A job of unknown type is converted to a TypeError error result."""
        result = pool._run(cast(AgentJob, object()))
        assert result.ok is False
        assert "TypeError" in (result.error or "")


class TestParse:
    """Tests for parse callable on AgentJob."""

    def test_parse_callable_applied(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Parse callable is invoked and result stored."""

        def my_parser(text: str) -> dict[str, object]:
            return {"parsed": text.upper()}

        job = _agent_job(prompt_builder=lambda: "prompt", parse=my_parser)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("hello world", "sid")
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"parsed": "HELLO WORLD"}

    def test_parse_callable_exception_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Parse callable failures are bounded by the shared cap."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)
        checkpoint = MagicMock()

        def bad_parser(text: str) -> object:
            assert checkpoint.call_count == 1
            raise ValueError("parse failed " + ("y" * 200))

        job = _agent_job(
            prompt_builder=lambda: "prompt",
            parse=bad_parser,
            session_key="plan-reviewer-cycle-01234567-89ab-cdef-0123-456789abcdef",
            session_checkpoint=checkpoint,
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("output", "sid")
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("parse failed: ValueError: ")
        assert len(result.error) == small_err_max
        assert result.session_id == "sid"
        checkpoint.assert_called_once_with("sid", None)


class TestInterruptedPostCheck:
    """Tests for the mandatory post-check interrupt flag."""

    def test_interrupted_post_check_on_shutdown_event(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown set WHILE the job runs -> post-check forces interrupted.

        The prompt builder blocks until the test sets the shutdown event, so
        the job is deterministically mid-flight when the event fires — this
        proves the POST-check path ran, not the before-start pre-check.
        """
        started = threading.Event()

        def blocking_builder() -> str:
            started.set()
            assert shutdown_event.wait(timeout=10)
            return "prompt"

        job = _agent_job(prompt_builder=blocking_builder)

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("done", "sid")
            pool.submit(job, StageName.PR_REVIEW)
            assert started.wait(timeout=10), "job never started"
            shutdown_event.set()
            _handle, result = completion_q.get(timeout=10)

        assert result.interrupted is True
        assert result.ok is False
        # Proves the POST-check ran: the pre-check path would have stamped
        # this sentinel error and never invoked the prompt builder.
        assert result.error != "interrupted_before_start"

    def test_interrupted_before_start(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
        completion_q: CompletionQueue,
    ) -> None:
        """Shutdown event set before job starts -> error and callable never invoked."""
        shutdown_event.set()

        job = _agent_job(prompt_builder=MagicMock())

        with patch(f"{_WP}.time.monotonic", side_effect=[10.0, 10.25]):
            pool.submit(job, StageName.PLANNING)
            _, result = completion_q.get(timeout=10)

        assert result.interrupted is True
        assert result.ok is False
        assert result.error == "interrupted_before_start"
        assert result.duration_s == pytest.approx(0.25)
        assert result.stdout_tail == ""
        assert result.stderr_tail == ""
        # Callable should never have been invoked (was MagicMock above)
        assert not job.prompt_builder.called  # type: ignore[attr-defined]


class TestGitOps:
    """Tests for every GitJob op dispatch (helpers mocked)."""

    @pytest.fixture(autouse=True)
    def _mock_trusted_gh_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Keep checkout-sync tests independent of the host's gh install layout."""

        def executable(_root: Path | None = None) -> str:
            return "/usr/bin/gh"

        monkeypatch.setattr(f"{_WP}._trusted_gh_executable", executable)
        monkeypatch.setattr(f"{__name__}._trusted_gh_executable", executable)

    def test_create_worktree_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """create_worktree forwards kwargs to WorktreeManager.create_worktree."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(
            base_dir=tmp_path / "build" / ".worktrees",
            repo_root=tmp_path,
        )
        instance.create_worktree.assert_called_once_with(
            issue_number=7,
            branch_name="7-auto",
            timeout=60,
        )
        assert result.ok is True
        assert result.value == str(tmp_path / "wt")

    def test_create_worktree_normal_reuse_returns_complete_dirty_snapshot(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A normal reused checkout cannot hide dirty state behind a path result."""
        worktree = tmp_path / "build" / ".worktrees" / "issue-2920"
        branch = "2920-auto-impl"
        head = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 2920,
                "branch_name": branch,
                "repo_root": str(tmp_path),
                "refresh_base": True,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = worktree
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: fixture\n", encoding="utf-8")

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args == ["git", "status", "--short"]:
                return subprocess.CompletedProcess(args, 0, stdout=" M changed.py\n?? new.py\n")
            if args == ["git", "diff"]:
                return subprocess.CompletedProcess(args, 0, stdout="+changed\n")
            if args == ["git", "rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(args, 0, stdout=f"{head}\n")
            if args == ["git", "branch", "--show-current"]:
                return subprocess.CompletedProcess(args, 0, stdout=f"{branch}\n")
            raise AssertionError(args)

        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=False),
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "path": str(worktree),
            "branch": branch,
            "head_sha": head,
            "dirty": True,
            "status": " M changed.py\n?? new.py\n",
            "diff": "+changed\n",
            "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
        }

    @pytest.mark.parametrize("changed_kind", ["staged", "untracked", "untracked_newline"])
    def test_recover_dirty_worktree_rejects_byte_drift_with_unchanged_status(
        self,
        pool: WorkerPool,
        tmp_path: Path,
        changed_kind: str,
    ) -> None:
        """Recovery stops when staged or untracked bytes change after capture."""
        repo = tmp_path / "repo"
        branch = "2920-auto-impl"

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        repo.mkdir()
        git("init", "-q", "-b", branch)
        git("config", "user.name", "Test User")
        git("config", "user.email", "test@example.invalid")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
        head = git("rev-parse", "HEAD").stdout.strip()
        changed_path = (
            repo
            / {
                "staged": "tracked.txt",
                "untracked": "new.txt",
                "untracked_newline": "new\nfile.txt",
            }[changed_kind]
        )
        changed_path.write_text("first bytes\n", encoding="utf-8")
        if changed_kind == "staged":
            git("add", "tracked.txt")

        captured = pool._finalize_created_worktree(
            created=repo,
            base_sha=None,
            branch_name=branch,
            repo_root=repo,
            repo="test/repo",
            sync_to_remote=False,
            pr_number=None,
            timeout_s=60,
        )
        assert captured.ok is True
        assert isinstance(captured.value, dict)
        assert "content_snapshot" in captured.value
        snapshot = cast(dict[str, str], captured.value["content_snapshot"])
        status = cast(str, captured.value["status"])
        diff = cast(str, captured.value["diff"])

        changed_path.write_text("second byte content\n", encoding="utf-8")
        if changed_kind == "staged":
            git("add", "tracked.txt")
        assert git("status", "--short").stdout == status
        assert git("diff").stdout == diff

        job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(repo),
                "worktree_path": str(repo),
                "branch": branch,
                "issue_number": 2920,
                "action": "COMMIT",
                "pre_action_head": head,
                "expected_remote_head": head,
                "status": status,
                "diff": diff,
                "content_snapshot": snapshot,
            },
        )
        with (
            patch.object(pool, "_read_remote_branch_head") as remote_probe,
            patch.object(
                pool,
                "_commit_if_changes_with_controlled_signing",
                return_value=JobResult(ok=False, error="must not run"),
            ) as commit,
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "worktree_content_drift"
        remote_probe.assert_not_called()
        commit.assert_not_called()

    @pytest.mark.parametrize("action", ["COMMIT", "STASH"])
    def test_recover_dirty_worktree_rejects_drift_during_remote_probe(
        self,
        pool: WorkerPool,
        tmp_path: Path,
        action: str,
    ) -> None:
        """Recovery rechecks bytes after its remote probe and before mutation."""
        repo = tmp_path / "repo"
        branch = "2920-auto-impl"

        def git(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            )

        repo.mkdir()
        git("init", "-q", "-b", branch)
        git("config", "user.name", "Test User")
        git("config", "user.email", "test@example.invalid")
        changed_path = repo / "tracked.txt"
        changed_path.write_text("base\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
        head = git("rev-parse", "HEAD").stdout.strip()
        changed_path.write_text("first bytes\n", encoding="utf-8")
        status = git("status", "--short").stdout
        diff = git("diff").stdout
        snapshot = _dirty_worktree_content_snapshot(repo, timeout=60)
        job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(repo),
                "worktree_path": str(repo),
                "branch": branch,
                "issue_number": 2920,
                "action": action,
                "pre_action_head": head,
                "expected_remote_head": head,
                "status": status,
                "diff": diff,
                "content_snapshot": snapshot,
            },
        )

        def remote_probe(*_args: object, **_kwargs: object) -> str:
            changed_path.write_text("second byte content\n", encoding="utf-8")
            return head

        real_git_run = git_utils.run
        with (
            patch(f"{_WP}.git_utils.run", wraps=real_git_run) as run_git,
            patch.object(pool, "_read_remote_branch_head", side_effect=remote_probe),
            patch.object(
                pool,
                "_commit_if_changes_with_controlled_signing",
                return_value=False,
            ) as commit,
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "worktree_content_drift"
        commit.assert_not_called()
        assert not any(
            invocation.args and invocation.args[0][:3] == ["git", "stash", "push"]
            for invocation in run_git.call_args_list
        )

    @staticmethod
    def _exercise_real_dirty_recovery_rebase(
        pool: WorkerPool,
        tmp_path: Path,
        *,
        advance_main: bool,
    ) -> tuple[JobResult, JobResult, Path, str]:
        """Publish a real recovery commit and pass its head to writer rebase."""
        origin = tmp_path / "origin.git"
        checkout = tmp_path / "checkout"
        signing_key = tmp_path / "signing-key"
        branch = "2920-auto-impl"

        def git(*args: str, cwd: Path = checkout) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )

        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "--quiet", "--initial-branch", "main", str(checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                _executable_path("ssh-keygen"),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(signing_key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (
            ("user.name", "Test User"),
            ("user.email", "test@example.invalid"),
            ("gpg.format", "ssh"),
            ("user.signingkey", str(signing_key)),
            ("commit.gpgsign", "false"),
        ):
            git("config", key, value)
        git("remote", "add", "origin", str(origin))
        (checkout / "recovered.txt").write_text("base\n", encoding="utf-8")
        git("add", "recovered.txt")
        git("commit", "--quiet", "--no-gpg-sign", "-m", "test: base")
        git("push", "--quiet", "-u", "origin", "main")
        git("switch", "--quiet", "-c", branch)
        git("push", "--quiet", "-u", "origin", branch)
        if advance_main:
            git("switch", "--quiet", "main")
            (checkout / "main.txt").write_text("new main\n", encoding="utf-8")
            git("add", "main.txt")
            git("commit", "--quiet", "--no-gpg-sign", "-m", "test: advance main")
            git("push", "--quiet", "origin", "main")
            git("switch", "--quiet", branch)

        pre_action_head = git("rev-parse", "HEAD").stdout.strip()
        (checkout / "recovered.txt").write_text("recovered bytes\n", encoding="utf-8")
        captured = pool._finalize_created_worktree(
            created=checkout,
            base_sha=None,
            branch_name=branch,
            repo_root=checkout,
            repo="test/repo",
            sync_to_remote=False,
            pr_number=None,
            timeout_s=60,
        )
        assert captured.ok is True and isinstance(captured.value, dict)

        def commit_recovery(*_args: object, **_kwargs: object) -> bool:
            git("add", "--all")
            git("commit", "--quiet", "-S", "-s", "-m", "fix: recover dirty writer")
            return True

        remote_configuration = (os.environ.copy(), ())

        def revalidate_remote() -> tuple[dict[str, str], tuple[str, ...]]:
            return remote_configuration

        recovery_job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(checkout),
                "worktree_path": str(checkout),
                "branch": branch,
                "issue_number": 2920,
                "action": "COMMIT",
                "pre_action_head": pre_action_head,
                "expected_remote_head": pre_action_head,
                "status": captured.value["status"],
                "diff": captured.value["diff"],
                "content_snapshot": captured.value["content_snapshot"],
            },
        )
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=remote_configuration,
            ),
            patch.object(
                pool,
                "_authenticated_remote_revalidator",
                return_value=revalidate_remote,
            ),
            patch.object(
                pool,
                "_commit_if_changes_with_controlled_signing",
                side_effect=commit_recovery,
            ),
        ):
            recovery = pool._git_recover_dirty_worktree(recovery_job)
        assert recovery.ok is True and isinstance(recovery.value, dict)
        recovered_head = cast(str, recovery.value["current_head"])

        signing = {
            "user.name": "Test User",
            "user.email": "test@example.invalid",
            "gpg.format": "ssh",
            "user.signingkey": str(signing_key),
        }
        rebase_job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": checkout,
                "base_branch": "main",
                "remote": "origin",
                "publish_rebased_head": True,
                "branch": branch,
                "expected_remote_sha": recovered_head,
            },
        )
        with (
            patch(f"{_WP}._read_host_git_signing_config", return_value=signing),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=remote_configuration,
            ),
            patch.object(
                pool,
                "_authenticated_remote_revalidator",
                return_value=revalidate_remote,
            ),
        ):
            rebase = pool._git_rebase(rebase_job)
        return recovery, rebase, checkout, branch

    @pytest.mark.usefixtures("require_git_path_format")
    def test_dirty_recovery_commit_feeds_noop_writer_rebase(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A published recovery head is the exact lease for a no-op rebase."""
        recovery, rebase, checkout, branch = self._exercise_real_dirty_recovery_rebase(
            pool,
            tmp_path,
            advance_main=False,
        )
        recovered_head = recovery.value["current_head"]

        assert rebase == JobResult(
            ok=True,
            value={
                "rebased": False,
                "published": False,
                "head_sha": recovered_head,
            },
        )
        remote_head = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        assert remote_head == recovered_head
        assert (checkout / "recovered.txt").read_text(encoding="utf-8") == "recovered bytes\n"

    @pytest.mark.usefixtures("require_git_path_format")
    def test_dirty_recovery_commit_is_lease_for_required_rebase(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A required rebase publishes from the exact recovery-head lease."""
        recovery, rebase, checkout, branch = self._exercise_real_dirty_recovery_rebase(
            pool,
            tmp_path,
            advance_main=True,
        )

        assert rebase.ok is True
        assert rebase.value["rebased"] is True
        assert rebase.value["published"] is True
        assert rebase.value["head_sha"] != recovery.value["current_head"]
        remote_head = subprocess.run(
            ["git", "ls-remote", "origin", f"refs/heads/{branch}"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        assert remote_head == rebase.value["head_sha"]
        assert (checkout / "recovered.txt").read_text(encoding="utf-8") == "recovered bytes\n"
        assert (checkout / "main.txt").read_text(encoding="utf-8") == "new main\n"

    def test_recover_dirty_worktree_stashes_untracked_content_and_proves_receipt(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """STASH preserves tracked and untracked changes without moving either head."""
        repo = tmp_path / "repo"
        worktree = repo / "build" / ".worktrees" / "issue-2920"
        repo.mkdir()

        def git(*args: str, cwd: Path = repo) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.name", "Test User")
        git("config", "user.email", "test@example.com")
        (repo / "tracked.txt").write_text("base\n", encoding="utf-8")
        git("add", "tracked.txt")
        git("commit", "-q", "--no-gpg-sign", "-m", "test: base")
        worktree.parent.mkdir(parents=True)
        git("worktree", "add", "-q", "-b", "2920-auto-impl", str(worktree))
        head = git("rev-parse", "HEAD", cwd=worktree).stdout.strip()
        (worktree / "tracked.txt").write_text("changed\n", encoding="utf-8")
        (worktree / "untracked.txt").write_text("preserve me\n", encoding="utf-8")
        status = git("status", "--short", cwd=worktree).stdout
        diff = git("diff", cwd=worktree).stdout
        content_snapshot = _dirty_worktree_content_snapshot(worktree, timeout=60)
        job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(repo),
                "worktree_path": str(worktree),
                "branch": "2920-auto-impl",
                "issue_number": 2920,
                "pr_number": 3000,
                "action": "STASH",
                "pre_action_head": head,
                "expected_remote_head": head,
                "status": status,
                "diff": diff,
                "content_snapshot": content_snapshot,
            },
        )

        with patch.object(pool, "_read_remote_branch_head", return_value=head):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is True
        assert result.value["outcome"] == "recovered"
        assert result.value["action"] == "STASH"
        assert result.value["action_applied"] is True
        assert result.value["published"] is False
        assert result.value["current_head"] == head
        assert result.value["remote_head"] == head
        assert result.value["final_clean"] is True
        stash_object = result.value["stash_object"]
        assert isinstance(stash_object, str) and len(stash_object) == 40
        stashed_paths = git(
            "stash", "show", "--include-untracked", "--name-only", "refs/stash", cwd=worktree
        ).stdout.splitlines()
        assert stashed_paths == ["tracked.txt", "untracked.txt"]

    def test_recover_dirty_worktree_commit_uses_controlled_signing_and_exact_lease(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """COMMIT proves controlled creation, metadata, publication, and postflight."""
        old_head = "a" * 40
        new_head = "b" * 40
        branch = "2920-auto-impl"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        status = " M changed.py\n"
        diff = "+changed\n"
        head_reads = iter((old_head, new_head))

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            outputs: dict[tuple[str, ...], str] = {
                ("git", "worktree", "list", "--porcelain"): (
                    f"worktree {worktree}\nHEAD {old_head}\nbranch refs/heads/{branch}\n"
                ),
                ("git", "branch", "--show-current"): f"{branch}\n",
                ("git", "status", "--short"): status,
                ("git", "diff"): diff,
                ("git", "rev-parse", "HEAD^"): f"{old_head}\n",
                ("git", "cat-file", "-p", new_head): (
                    "tree deadbeef\ngpgsig signed\n\nfix\n\n"
                    "Signed-off-by: Test User <test@example.com>\n"
                ),
            }
            if args == ["git", "rev-parse", "HEAD"]:
                output = f"{next(head_reads)}\n"
            else:
                output = outputs[tuple(args)]
            return subprocess.CompletedProcess(args, 0, stdout=output)

        job = GitJob(
            repo="test/repo",
            expected_repository="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(tmp_path),
                "worktree_path": str(worktree),
                "branch": branch,
                "issue_number": 2920,
                "pr_number": 3000,
                "action": "COMMIT",
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": status,
                "diff": diff,
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
                "agent": "codex",
                "agent_model": "gpt-test",
                "git_message_timeout": 123,
            },
        )
        revalidate = MagicMock(return_value=({"AUTH": "1"}, ["-c", "credential.helper="]))

        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
            patch.object(
                pool, "_commit_if_changes_with_controlled_signing", return_value=True
            ) as commit,
            patch.object(pool, "_read_publish_head", return_value=new_head),
            patch.object(pool, "_read_remote_branch_head", side_effect=[old_head, new_head]),
            patch.object(pool, "_authenticated_remote_revalidator", return_value=revalidate),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is True
        commit.assert_called_once_with(
            job,
            (2920, worktree, "codex"),
            None,
            "gpt-test",
            123,
        )
        push.assert_called_once_with(
            branch,
            old_head,
            worktree,
            source_sha=new_head,
            timeout=60,
            env={"AUTH": "1"},
            remote_config=["-c", "credential.helper="],
            revalidate_remote=revalidate,
        )
        assert result.value == {
            "outcome": "recovered",
            "failure_kind": None,
            "action": "COMMIT",
            "branch": branch,
            "worktree_path": str(worktree),
            "pre_action_head": old_head,
            "current_head": new_head,
            "expected_remote_head": old_head,
            "remote_head": new_head,
            "published": True,
            "stash_object": None,
            "action_applied": True,
            "final_clean": True,
            "cause": "",
        }

    @pytest.mark.parametrize(
        ("parent", "raw_commit"),
        [
            ("c" * 40, "tree x\ngpgsig signed\n\nSigned-off-by: Test <t@example.com>\n"),
            ("a" * 40, "tree x\n\nSigned-off-by: Test <t@example.com>\n"),
            ("a" * 40, "tree x\ngpgsig signed\n\nmissing trailer\n"),
        ],
    )
    def test_recover_dirty_worktree_rejects_invalid_commit_metadata_before_push(
        self,
        pool: WorkerPool,
        tmp_path: Path,
        parent: str,
        raw_commit: str,
    ) -> None:
        """Parent, signature, and DCO proof are each required before publication."""
        old_head = "a" * 40
        new_head = "b" * 40
        branch = "2920-auto-impl"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        status = " M changed.py\n"
        diff = "+changed\n"

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            outputs = {
                ("git", "worktree", "list", "--porcelain"): (
                    f"worktree {worktree}\nHEAD {old_head}\nbranch refs/heads/{branch}\n"
                ),
                ("git", "rev-parse", "HEAD"): f"{old_head}\n",
                ("git", "branch", "--show-current"): f"{branch}\n",
                ("git", "status", "--short"): status,
                ("git", "diff"): diff,
                ("git", "rev-parse", "HEAD^"): f"{parent}\n",
                ("git", "cat-file", "-p", new_head): raw_commit,
            }
            return subprocess.CompletedProcess(args, 0, stdout=outputs[tuple(args)])

        job = GitJob(
            repo="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(tmp_path),
                "worktree_path": str(worktree),
                "branch": branch,
                "issue_number": 2920,
                "action": "COMMIT",
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": status,
                "diff": diff,
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
            },
        )
        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
            patch.object(pool, "_read_remote_branch_head", return_value=old_head),
            patch.object(pool, "_commit_if_changes_with_controlled_signing", return_value=True),
            patch.object(pool, "_read_publish_head", return_value=new_head),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "commit_metadata_invalid"
        assert result.value["action_applied"] is True
        assert result.value["current_head"] == new_head
        push.assert_not_called()

    def test_recover_dirty_worktree_invalid_identity_does_not_mutate(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A registration mismatch stops before commit, stash, or publication."""
        old_head = "a" * 40
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        commands: list[list[str]] = []

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            commands.append(args)
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=f"worktree {worktree}\nHEAD {'c' * 40}\nbranch refs/heads/other\n",
            )

        job = GitJob(
            repo="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(tmp_path),
                "worktree_path": str(worktree),
                "branch": "2920-auto-impl",
                "issue_number": 2920,
                "action": "COMMIT",
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": " M changed.py\n",
                "diff": "+changed\n",
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
            },
        )
        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch.object(pool, "_commit_if_changes_with_controlled_signing") as commit,
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "worktree_identity_drift"
        assert result.value["action_applied"] is False
        assert commands == [["git", "worktree", "list", "--porcelain"]]
        commit.assert_not_called()
        push.assert_not_called()

    def test_recover_dirty_worktree_publication_failure_retains_local_commit_evidence(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A failed exact-lease push retains the created commit in its receipt."""
        old_head = "a" * 40
        new_head = "b" * 40
        branch = "2920-auto-impl"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        status = " M changed.py\n"
        diff = "+changed\n"

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            outputs = {
                ("git", "worktree", "list", "--porcelain"): (
                    f"worktree {worktree}\nHEAD {old_head}\nbranch refs/heads/{branch}\n"
                ),
                ("git", "rev-parse", "HEAD"): f"{old_head}\n",
                ("git", "branch", "--show-current"): f"{branch}\n",
                ("git", "status", "--short"): status,
                ("git", "diff"): diff,
                ("git", "rev-parse", "HEAD^"): f"{old_head}\n",
                ("git", "cat-file", "-p", new_head): (
                    "tree x\ngpgsig signed\n\nSigned-off-by: Test <t@example.com>\n"
                ),
            }
            return subprocess.CompletedProcess(args, 0, stdout=outputs[tuple(args)])

        job = GitJob(
            repo="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(tmp_path),
                "worktree_path": str(worktree),
                "branch": branch,
                "issue_number": 2920,
                "action": "COMMIT",
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": status,
                "diff": diff,
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
            },
        )
        revalidate = MagicMock(return_value=({}, []))
        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
            patch.object(pool, "_read_remote_branch_head", return_value=old_head),
            patch.object(pool, "_commit_if_changes_with_controlled_signing", return_value=True),
            patch.object(pool, "_read_publish_head", return_value=new_head),
            patch.object(pool, "_authenticated_remote_revalidator", return_value=revalidate),
            patch(
                f"{_WP}.git_utils.push_head_to_branch",
                side_effect=RuntimeError("lease rejected"),
            ),
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "operation_failed"
        assert result.value["action_applied"] is True
        assert result.value["current_head"] == new_head
        assert result.value["published"] is False

    def test_recover_dirty_worktree_poststash_failure_retains_stash_object(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A postflight failure retains the new stash object as recovery evidence."""
        old_head = "a" * 40
        stash_object = "d" * 40
        branch = "2920-auto-impl"
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        (worktree / ".git").mkdir()
        head_reads = iter((old_head, old_head))

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            outputs = {
                ("git", "worktree", "list", "--porcelain"): (
                    f"worktree {worktree}\nHEAD {old_head}\nbranch refs/heads/{branch}\n"
                ),
                ("git", "branch", "--show-current"): f"{branch}\n",
                ("git", "status", "--short"): " M changed.py\n",
                ("git", "diff"): "+changed\n",
                ("git", "rev-parse", "--verify", "-q", "refs/stash"): "",
                (
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "hephaestus dirty recovery issue #2920",
                ): "Saved\n",
                ("git", "rev-parse", "--verify", "refs/stash"): f"{stash_object}\n",
            }
            output = (
                f"{next(head_reads)}\n"
                if args == ["git", "rev-parse", "HEAD"]
                else outputs[tuple(args)]
            )
            return subprocess.CompletedProcess(args, 0, stdout=output)

        job = GitJob(
            repo="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(tmp_path),
                "worktree_path": str(worktree),
                "branch": branch,
                "issue_number": 2920,
                "action": "STASH",
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": " M changed.py\n",
                "diff": "+changed\n",
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
            },
        )
        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
            patch.object(pool, "_read_remote_branch_head", side_effect=[old_head, "c" * 40]),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["failure_kind"] == "remote_postflight_drift"
        assert result.value["action_applied"] is True
        assert result.value["stash_object"] == stash_object
        assert result.value["current_head"] == old_head

    @pytest.mark.parametrize(
        ("scenario", "failure_kind"),
        [
            ("invalid_request", "invalid_request"),
            ("unavailable", "worktree_unavailable"),
            ("unconfined", "worktree_unconfined"),
            ("clean", "worktree_clean"),
            ("content_drift", "worktree_content_drift"),
            ("remote_probe", "remote_probe_failed"),
            ("remote_drift", "remote_head_drift"),
            ("commit_result", "commit_failed"),
            ("commit_false", "commit_failed"),
            ("commit_head", "commit_postflight_failed"),
            ("stash_invalid", "stash_evidence_invalid"),
            ("postflight_dirty", "postflight_dirty"),
            ("remote_postflight_probe", "remote_postflight_failed"),
            ("stash_head_drift", "stash_head_drift"),
        ],
    )
    def test_recover_dirty_worktree_returns_structured_failure_at_each_boundary(  # noqa: C901
        self,
        pool: WorkerPool,
        tmp_path: Path,
        scenario: str,
        failure_kind: str,
    ) -> None:
        """Each recovery boundary returns its named structured failure receipt."""
        old_head = "a" * 40
        new_head = "b" * 40
        stash_object = "d" * 40
        branch = "2920-auto-impl"
        repo = tmp_path / "repo"
        repo.mkdir()
        worktree = repo / "worktree"
        if scenario == "unavailable":
            worktree = repo / "missing"
        elif scenario == "unconfined":
            worktree = tmp_path / "outside"
            worktree.mkdir()
            (worktree / ".git").mkdir()
        else:
            worktree.mkdir()
            (worktree / ".git").mkdir()
        action = (
            "STASH"
            if scenario.startswith("stash") or scenario == "remote_postflight_probe"
            else "COMMIT"
        )
        expected_status = " M changed.py\n"
        expected_diff = "+changed\n"
        head_reads = 0

        def run_git(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal head_reads
            outputs = {
                ("git", "worktree", "list", "--porcelain"): (
                    f"worktree {worktree}\nHEAD {old_head}\nbranch refs/heads/{branch}\n"
                ),
                ("git", "branch", "--show-current"): f"{branch}\n",
                ("git", "status", "--short"): "" if scenario == "clean" else expected_status,
                ("git", "diff"): "different\n" if scenario == "content_drift" else expected_diff,
                ("git", "rev-parse", "HEAD^"): f"{old_head}\n",
                ("git", "cat-file", "-p", new_head): (
                    "tree x\ngpgsig signed\n\nSigned-off-by: Test <t@example.com>\n"
                ),
                ("git", "rev-parse", "--verify", "-q", "refs/stash"): "",
                (
                    "git",
                    "stash",
                    "push",
                    "--include-untracked",
                    "-m",
                    "hephaestus dirty recovery issue #2920",
                ): "Saved\n",
                ("git", "rev-parse", "--verify", "refs/stash"): (
                    "bad\n" if scenario == "stash_invalid" else f"{stash_object}\n"
                ),
            }
            if args == ["git", "rev-parse", "HEAD"]:
                head_reads += 1
                if head_reads == 1:
                    output = f"{old_head}\n"
                elif action == "COMMIT":
                    output = f"{new_head}\n"
                elif scenario == "stash_head_drift":
                    output = f"{'c' * 40}\n"
                else:
                    output = f"{old_head}\n"
            else:
                output = outputs[tuple(args)]
            return subprocess.CompletedProcess(args, 0, stdout=output)

        job = GitJob(
            repo="test/repo",
            op="recover_dirty_worktree",
            timeout_s=60,
            kwargs={
                "repo_root": str(repo),
                "worktree_path": str(worktree),
                "branch": branch,
                "issue_number": 2920,
                "action": "BAD" if scenario == "invalid_request" else action,
                "pre_action_head": old_head,
                "expected_remote_head": old_head,
                "status": expected_status,
                "diff": expected_diff,
                "content_snapshot": _DIRTY_CONTENT_SNAPSHOT,
            },
        )
        if scenario == "remote_probe":
            remote_results: list[str | JobResult] = [JobResult(ok=False, error="probe failed")]
        elif scenario == "remote_drift":
            remote_results = ["c" * 40]
        elif scenario == "remote_postflight_probe":
            remote_results = [old_head, JobResult(ok=False, error="probe failed")]
        else:
            remote_results = [old_head, new_head if action == "COMMIT" else old_head]
        commit_result: bool | JobResult = True
        if scenario == "commit_result":
            commit_result = JobResult(ok=False, error="commit failed")
        elif scenario == "commit_false":
            commit_result = False
        publish_head: str | JobResult = (
            JobResult(ok=False, error="head failed") if scenario == "commit_head" else new_head
        )
        revalidate = MagicMock(return_value=({}, []))
        with (
            patch(f"{_WP}.git_utils.run", side_effect=run_git),
            patch(
                f"{_WP}._dirty_worktree_content_snapshot",
                return_value=_DIRTY_CONTENT_SNAPSHOT,
            ),
            patch.object(pool, "_read_remote_branch_head", side_effect=remote_results),
            patch.object(
                pool,
                "_commit_if_changes_with_controlled_signing",
                return_value=commit_result,
            ),
            patch.object(pool, "_read_publish_head", return_value=publish_head),
            patch.object(pool, "_authenticated_remote_revalidator", return_value=revalidate),
            patch(f"{_WP}.git_utils.push_head_to_branch"),
            patch(
                f"{_WP}.git_utils.is_clean_working_tree",
                return_value=scenario != "postflight_dirty",
            ),
        ):
            result = pool._git_recover_dirty_worktree(job)

        assert result.ok is False
        assert result.value["outcome"] == "failed"
        assert result.value["failure_kind"] == failure_kind
        assert set(result.value) == {
            "outcome",
            "failure_kind",
            "action",
            "branch",
            "worktree_path",
            "pre_action_head",
            "current_head",
            "expected_remote_head",
            "remote_head",
            "published",
            "stash_object",
            "action_applied",
            "final_clean",
            "cause",
        }

    def test_create_worktree_uses_canonical_repository_for_remote_validation(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A short scheduler key never becomes the authenticated origin identity."""
        job = GitJob(
            repo="Hephaestus",
            expected_repository="HomericIntelligence/Hephaestus",
            op="create_worktree",
            timeout_s=60,
            kwargs={"issue_number": 2912, "branch_name": "2912-auto", "repo_root": str(tmp_path)},
        )
        manager = MagicMock()
        manager.create_worktree.return_value = None

        with (
            patch.object(
                pool,
                "_prepare_direct_scope_worktree",
                return_value=(None, "2912-auto"),
            ) as prepare,
            patch(f"{_WP}.WorktreeManager", return_value=manager),
        ):
            result = pool._git_create_worktree(job)

        assert result.ok is True
        assert result.value is None
        assert prepare.call_args.kwargs["expected_repo"] == "HomericIntelligence/Hephaestus"

    def test_create_worktree_authenticates_requested_remote_refresh(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """The worker gives a remote refresh an isolated GitHub credential helper."""
        job = GitJob(
            repo="Hephaestus",
            expected_repository="HomericIntelligence/Hephaestus",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 2924,
                "branch_name": "2924-auto",
                "repo_root": str(tmp_path),
                "refresh_base": True,
            },
        )
        manager = MagicMock()
        manager.create_worktree.return_value = tmp_path / "build" / ".worktrees" / "issue-2924"
        remote_env = {"GIT_TERMINAL_PROMPT": "0"}
        remote_config = ("-c", "credential.helper=!trusted-gh auth git-credential")

        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ) as authenticate,
            patch(f"{_WP}.WorktreeManager", return_value=manager) as manager_type,
        ):
            result = pool._git_create_worktree(job)

        assert result.ok is True
        authenticate.assert_called_once_with(
            cwd=tmp_path,
            expected_repo="HomericIntelligence/Hephaestus",
            timeout=60,
        )
        manager_type.assert_called_once_with(
            base_dir=tmp_path / "build" / ".worktrees",
            repo_root=tmp_path,
            remote_git_env=remote_env,
            remote_git_config=remote_config,
        )

    def test_create_worktree_remote_refresh_failure_is_safe_and_classified(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A failed refresh does not copy remote output into the job result."""
        sensitive_value = "ghp_1234567890abcdefghijklmnopqrstuvwxyzABCDE"
        job = GitJob(
            repo="Hephaestus",
            expected_repository="HomericIntelligence/Hephaestus",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 2924,
                "branch_name": "2924-auto",
                "repo_root": str(tmp_path),
                "refresh_base": True,
            },
        )
        manager = MagicMock()
        manager.create_worktree.side_effect = subprocess.CalledProcessError(
            128,
            ["git", "fetch", "origin"],
            stderr=f"access denied for {sensitive_value}",
        )

        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=({}, ()),
            ),
            patch(f"{_WP}.WorktreeManager", return_value=manager),
        ):
            result = pool._run_git(job)

        assert result.ok is False
        assert result.value == {"failure_kind": "remote_git_transport"}
        assert result.error == "worktree remote refresh failed"
        assert sensitive_value not in result.stderr_tail

    def test_create_worktree_reports_existing_branch_owner(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Branch ownership is a stable structured result for the stage."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 2269,
                "branch_name": "shared-head",
                "repo_root": str(tmp_path),
            },
        )
        owner_path = tmp_path / "build" / ".worktrees" / "issue-2268"
        instance = MagicMock()
        instance.create_worktree.side_effect = BranchWorktreeOwnedError("shared-head", owner_path)
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == BRANCH_WORKTREE_OWNED
        assert result.value == {
            "branch": "shared-head",
            "owner_path": str(owner_path),
        }

    def test_direct_pinned_worktree_rejects_checkout_head_drift(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A direct-scope worktree never falls back when its checkout moved."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager,
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="b" * 40 + "\n"),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "direct scope checkout pin mismatch"
        mock_manager.assert_not_called()

    def test_direct_pinned_worktree_reserves_remote_branch_before_agent_admission(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A direct scope creates a server-side branch lease before returning a worktree."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.reserve_remote_branch_if_absent") as reserve,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        reserve.assert_called_once_with(
            "7-auto",
            pinned_sha,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
        )
        assert result.value == {
            "path": str(tmp_path / "wt"),
            "direct_scope_reservation": {
                "branch": "7-auto",
                "base_sha": pinned_sha,
            },
        }

    def test_direct_pinned_impl_writer_reserves_creates_and_claims(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A direct writer claims its source lane after its remote reservation."""
        pinned_sha = "a" * 40
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        authority = MagicMock(spec=ImplementationWriterAuthority)
        binding = MagicMock(revision=pinned_sha)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
                "source_lane": "impl",
            },
        )
        manager = MagicMock()
        manager.create_worktree.return_value = writer_path
        manager.implementation_writer_authority.return_value = authority
        source_manager = MagicMock()
        source_manager.claim_implementation_writer.return_value = binding

        with (
            patch(f"{_WP}.WorktreeManager", return_value=manager),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.reserve_remote_branch_if_absent") as reserve,
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        reserve.assert_called_once_with(
            "7-auto",
            pinned_sha,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
        )
        manager.create_worktree.assert_called_once_with(
            issue_number=7,
            branch_name="7-auto",
            refresh_base=False,
            base_sha=pinned_sha,
            source_lane="impl",
            remote_branch_reserved=True,
            implementation_writer_handoff=ANY,
            timeout=60,
        )
        manager.implementation_writer_authority.assert_called_once_with(writer_path)
        source_manager.claim_implementation_writer.assert_called_once_with(
            7,
            branch="7-auto",
            path=writer_path,
            authority=authority,
            handoff=ANY,
        )
        assert result.ok is True
        assert result.value == {
            "path": str(writer_path),
            "impl_source_revision": pinned_sha,
            "direct_scope_reservation": {"branch": "7-auto", "base_sha": pinned_sha},
        }

    def test_implementation_source_lane_rejects_unmaterialized_writer(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """An implementation lane cannot succeed when no writer was created."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        manager = MagicMock()
        manager.create_worktree.return_value = None

        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=({}, ()),
            ),
            patch(f"{_WP}.WorktreeManager", return_value=manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=MagicMock()),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == (
            "source_workspace_ownership_unavailable: implementation writer was not materialized"
        )
        assert result.value == {
            "path": str(tmp_path / "build" / ".worktrees" / "auto-7-impl"),
            WORKTREE_MATERIALIZED_KEY: False,
        }

    def test_implementation_source_lane_rejects_missing_clean_writer_path(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A planned writer path cannot bypass implementation ownership checks."""
        planned_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"

        result = pool._finalize_created_worktree(
            created=planned_path,
            base_sha=None,
            branch_name="7-auto",
            repo_root=tmp_path,
            repo="test/repo",
            sync_to_remote=False,
            pr_number=None,
            timeout_s=60,
            source_lane="impl",
            item_number=7,
            writer_authority=MagicMock(spec=ImplementationWriterAuthority),
        )

        assert result.ok is False
        assert result.error == (
            "source_workspace_ownership_unavailable: implementation writer was not materialized"
        )
        assert result.value == {
            "path": str(planned_path),
            WORKTREE_MATERIALIZED_KEY: False,
        }

    def test_direct_pinned_worktree_releases_reservation_when_no_worktree_is_created(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A manager no-op cannot leave the server-side reservation behind."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = None
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(f"{_WP}.git_utils.reserve_remote_branch_if_absent"),
            patch(
                f"{_WP}.git_utils.delete_reserved_branch_if_unchanged", return_value=True
            ) as release,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree manager returned no worktree"
        release.assert_called_once_with(
            "7-auto",
            pinned_sha,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
            revalidate_remote=ANY,
        )

    def test_direct_worktree_rollback_failure_preserves_reservation_receipt(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A retryable early rollback failure remains recoverable in Finished."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.side_effect = RuntimeError("disk failure")
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(f"{_WP}.git_utils.reserve_remote_branch_if_absent"),
            patch(
                f"{_WP}.git_utils.delete_reserved_branch_if_unchanged",
                side_effect=RuntimeError("network unavailable"),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {
            "direct_scope_reservation": {"branch": "7-auto", "base_sha": pinned_sha}
        }

    def test_direct_worktree_rollback_timeout_preserves_reservation_receipt(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A transport timeout must reach Finished's bounded release protocol."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.side_effect = RuntimeError("disk failure")
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(f"{_WP}.git_utils.reserve_remote_branch_if_absent"),
            patch(
                f"{_WP}.git_utils.delete_reserved_branch_if_unchanged",
                side_effect=subprocess.TimeoutExpired(["git", "push"], 60),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {
            "direct_scope_reservation": {"branch": "7-auto", "base_sha": pinned_sha}
        }

    def test_direct_pinned_worktree_fails_before_agent_admission_when_reservation_loses_race(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A confirmed remote branch owner yields a typed terminal result."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(
                f"{_WP}.git_utils.reserve_remote_branch_if_absent",
                side_effect=git_utils.DirectBranchReservationCollisionError("7-auto"),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "direct_scope_reservation_collision"
        assert result.value == {"direct_scope_reservation_collision": {"branch": "7-auto"}}

    def test_direct_pinned_worktree_keeps_unproven_reservation_failure_retryable(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A transport failure is not mislabeled as another run's branch collision."""
        pinned_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "refresh_base": False,
                "base_sha": pinned_sha,
            },
        )
        with (
            patch(
                f"{_WP}.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout=pinned_sha + "\n"),
            ),
            patch(
                f"{_WP}.git_utils.reserve_remote_branch_if_absent",
                side_effect=RuntimeError("network unavailable"),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "RuntimeError: network unavailable"
        assert result.value is None

    def test_create_worktree_syncs_adopted_clean_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """sync_to_remote is a worker concern, not leaked into WorktreeManager."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-existing",
                "refresh_base": False,
                "repo_root": str(tmp_path),
                "sync_to_remote": True,
                "pr_number": 70,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True) as mock_clean,
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        instance.create_worktree.assert_called_once_with(
            issue_number=7,
            branch_name="7-existing",
            refresh_base=False,
            timeout=60,
        )
        mock_clean.assert_called_once_with(tmp_path / "wt", timeout=60)
        mock_sync.assert_called_once()
        assert mock_sync.call_args.args == (tmp_path / "wt", "7-existing")
        assert mock_sync.call_args.kwargs["pr_number"] == 70
        assert mock_sync.call_args.kwargs["timeout"] == 60
        assert result.ok is True
        assert result.value == {
            "path": str(tmp_path / "wt"),
            "dirty": False,
            "status": "",
            "diff": "",
        }

    def test_create_worktree_sync_failure_retains_materialized_checkout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A post-create adopted sync error preserves first-writer evidence."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-existing",
                "refresh_base": False,
                "repo_root": str(tmp_path),
                "sync_to_remote": True,
                "pr_number": 70,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "wt"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(
                f"{_WP}.git_utils.sync_worktree_to_remote_branch",
                side_effect=RuntimeError("sync timeout"),
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {
            "path": str(tmp_path / "wt"),
            WORKTREE_MATERIALIZED_KEY: True,
        }
        assert "post-create preparation failed" in (result.error or "")

    def test_create_implementation_source_lane_claims_writer_ownership(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A controlled implementation writer is registered before source use."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
                "agent": "codex",
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.return_value = writer_path
        authority = ImplementationWriterAuthority("authority-token")
        worktree_manager.implementation_writer_authority.return_value = authority
        source_manager = MagicMock()
        source_manager.claim_implementation_writer.return_value.revision = "b" * 40
        with (
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager) as source_class,
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch.object(
                WorkerPool,
                "_trusted_git_metadata_receipt",
                return_value={"git_metadata_receipt": "b" * 64},
            ),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        source_class.assert_called_once_with(
            tmp_path,
            repository="test/repo",
            base_dir=writer_path.parent,
        )
        source_manager.claim_implementation_writer.assert_called_once_with(
            7,
            branch="7-auto",
            path=writer_path,
            authority=authority,
            handoff=ANY,
        )
        worktree_manager.implementation_writer_authority.assert_called_once_with(writer_path)
        assert result.ok is True
        assert result.value == {
            "path": str(writer_path),
            "impl_source_revision": "b" * 40,
            "git_metadata_receipt": "b" * 64,
        }

    def test_create_implementation_source_lane_handoff_uses_job_repository_identity(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Source ownership uses the stage repository, not its transport target."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="org/repo",
            op="create_worktree",
            timeout_s=60,
            expected_repository="transport/repo",
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.return_value = writer_path
        source_manager = MagicMock()
        with (
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager) as source_class,
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        source_class.assert_called_once_with(
            tmp_path,
            repository="org/repo",
            base_dir=writer_path.parent,
        )
        assert result.ok is True

    @pytest.mark.parametrize("base_sha", [None, "a" * 40], ids=["fresh", "direct"])
    def test_create_implementation_writer_passes_authenticated_transport_to_manager(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        base_sha: str | None,
    ) -> None:
        """Fresh and direct writer requests use controlled remote Git transport."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        remote_env = {"GIT_TERMINAL_PROMPT": "0"}
        remote_config = ("-c", "credential.helper=!trusted-gh auth git-credential")
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.return_value = writer_path
        authority = ImplementationWriterAuthority("authority-token")
        worktree_manager.implementation_writer_authority.return_value = authority
        source_manager = MagicMock()
        source_manager.claim_implementation_writer.return_value.revision = "b" * 40
        with (
            patch.object(pool, "_prepare_direct_scope_worktree", return_value=(base_sha, "7-auto")),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ) as authentication,
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager) as manager_class,
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        authentication.assert_called_once_with(
            cwd=tmp_path,
            expected_repo="test/repo",
            timeout=60,
        )
        manager_kwargs = manager_class.call_args.kwargs
        assert manager_kwargs["remote_git_env"] == remote_env
        assert manager_kwargs["remote_git_config"] == remote_config
        if base_sha is None:
            assert "base_branch" not in manager_kwargs
        else:
            assert manager_kwargs["base_branch"] == base_sha
        assert result.ok is True

    def test_create_implementation_source_lane_returns_typed_claim_failure(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A failed writer handoff does not become a generic Git error."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.return_value = writer_path
        source_manager = MagicMock()
        source_manager.claim_implementation_writer.side_effect = SourceWorkspaceError("mismatch")
        with (
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "source_workspace_ownership_unavailable: mismatch"
        assert result.value == {
            "path": str(writer_path),
            WORKTREE_MATERIALIZED_KEY: True,
        }

    def test_adopted_writer_head_drift_prevents_authority_mint_and_claim(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A post-sync head move preserves the writer and does not claim its lane."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        manager = MagicMock()
        manager.mint_adopted_implementation_writer_authority.side_effect = (
            WorktreeCreationReceiptError("implementation writer adoption head changed")
        )
        source_manager = MagicMock()

        with (
            patch.object(pool, "_sync_worktree_to_remote_branch") as sync,
            patch(f"{_WP}.SourceWorkspaceManager", return_value=source_manager) as source_class,
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
        ):
            result = pool._finalize_created_worktree(
                created=writer_path,
                base_sha=None,
                branch_name="7-adopted",
                repo_root=tmp_path,
                repo="test/repo",
                source_repository="test/repo",
                sync_to_remote=True,
                pr_number=700,
                timeout_s=60,
                source_lane="impl",
                item_number=7,
                worktree_manager=manager,
                implementation_adoption_head="a" * 40,
            )

        sync.assert_called_once()
        manager.mint_adopted_implementation_writer_authority.assert_called_once_with(
            issue_number=7,
            branch_name="7-adopted",
            worktree_path=writer_path,
            expected_head="a" * 40,
            timeout=60,
        )
        source_class.assert_not_called()
        source_manager.claim_implementation_writer.assert_not_called()
        assert result.ok is False
        assert result.error == (
            "source_workspace_ownership_unavailable: implementation writer adoption head changed"
        )
        assert result.value == {
            "path": str(writer_path),
            WORKTREE_MATERIALIZED_KEY: True,
        }

    @pytest.mark.parametrize("source_lane", [None, "review"])
    def test_nonimplementation_sync_never_requires_writer_authority(
        self,
        pool: WorkerPool,
        tmp_path: Path,
        source_lane: str | None,
    ) -> None:
        """A review or normal checkout can synchronize without writer authority."""
        checkout = tmp_path / "build" / ".worktrees" / "review-7"
        checkout.mkdir(parents=True)
        manager = MagicMock()

        with (
            patch.object(pool, "_sync_worktree_to_remote_branch") as sync,
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.SourceWorkspaceManager") as source_manager,
        ):
            result = pool._finalize_created_worktree(
                created=checkout,
                base_sha=None,
                branch_name="7-review",
                repo_root=tmp_path,
                repo="test/repo",
                sync_to_remote=True,
                pr_number=700,
                source_lane=source_lane,
                item_number=7,
                worktree_manager=manager,
                timeout_s=60,
            )

        assert result.ok is True
        sync.assert_called_once()
        manager.mint_adopted_implementation_writer_authority.assert_not_called()
        source_manager.assert_not_called()

    def test_create_implementation_source_lane_returns_typed_receipt_failure(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """An unreceipted existing writer reaches the ownership terminal path."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.side_effect = WorktreeCreationReceiptError(
            "deterministic implementation worktree has no creation receipt"
        )
        with (
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=MagicMock()),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == (
            "source_workspace_ownership_unavailable: "
            "deterministic implementation worktree has no creation receipt"
        )
        assert result.value == {
            "path": str(writer_path),
            WORKTREE_MATERIALIZED_KEY: True,
        }

    def test_create_implementation_source_lane_returns_typed_receipt_write_failure(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Receipt write errors preserve the deterministic writer for recovery."""
        writer_path = tmp_path / "build" / ".worktrees" / "auto-7-impl"
        writer_path.mkdir(parents=True)
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.side_effect = WorktreeCreationReceiptError("disk full")
        with (
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=MagicMock()),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "source_workspace_ownership_unavailable: disk full"
        assert result.value == {
            "path": str(writer_path),
            WORKTREE_MATERIALIZED_KEY: True,
        }

    def test_direct_writer_receipt_failure_keeps_remote_reservation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A failed direct writer leaves a receipt for bounded remote cleanup."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 7,
                "branch_name": "7-auto",
                "repo_root": str(tmp_path),
                "source_lane": "impl",
                "base_sha": pin,
            },
        )
        worktree_manager = MagicMock()
        worktree_manager.create_worktree.side_effect = WorktreeCreationReceiptError("disk full")
        with (
            patch.object(pool, "_prepare_direct_scope_worktree", return_value=(pin, "7-auto")),
            patch(f"{_WP}.WorktreeManager", return_value=worktree_manager),
            patch(f"{_WP}.SourceWorkspaceManager", return_value=MagicMock()),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {
            "path": str(tmp_path / "build" / ".worktrees" / "auto-7-impl"),
            WORKTREE_MATERIALIZED_KEY: False,
            "direct_scope_reservation": {"branch": "7-auto", "base_sha": pin},
        }

    def test_create_isolated_worktree_syncs_only_detached_checkout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """PR review syncs its returned detached path, never a writer checkout."""
        review_path = tmp_path / "build" / ".worktrees" / "pr-review-pr-70"
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 70,
                "branch_name": "70-existing",
                "isolated": True,
                "repo_root": str(tmp_path),
                "sync_to_remote": True,
                "pr_number": 70,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = review_path
        instance.last_isolated_recovery_paths = []
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        instance.create_worktree.assert_called_once_with(
            issue_number=70,
            branch_name="70-existing",
            isolated=True,
            timeout=60,
        )
        mock_sync.assert_called_once()
        assert mock_sync.call_args.args == (review_path, "70-existing")
        assert mock_sync.call_args.kwargs["pr_number"] == 70
        assert mock_sync.call_args.kwargs["timeout"] == 60
        assert result.ok is True

    def test_create_isolated_worktree_does_not_infer_recovery_from_occupancy(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """An occupied checkout is not itself proof of an abandoned recovery."""
        review_path = tmp_path / "build" / ".worktrees" / "review-pr-70-1"
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 70,
                "branch_name": "70-existing",
                "isolated": True,
                "repo_root": str(tmp_path),
                "sync_to_remote": True,
                "pr_number": 70,
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = review_path
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance),
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch"),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "path": str(review_path),
            "dirty": False,
            "status": "",
            "diff": "",
        }

    def test_verify_pr_review_checkout_rejects_a_dirty_worktree(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A reviewer never receives a stale or locally dirty checkout."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=False),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"ready": False, "reason": "dirty"}
        mock_sync.assert_not_called()

    def test_verify_pr_review_checkout_retries_when_remote_head_drifted(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A post-sync HEAD mismatch is an explicit bounded re-review signal."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
            patch(f"{_WP}.git_utils.run", return_value=MagicMock(stdout="b" * 40 + "\n")),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"ready": False, "reason": "head_drift"}
        mock_sync.assert_called_once()
        assert mock_sync.call_args.args == (tmp_path, "70-existing")
        assert mock_sync.call_args.kwargs["pr_number"] == 70
        assert mock_sync.call_args.kwargs["timeout"] == 60

    def test_verify_pr_review_checkout_syncs_with_trusted_gh_credential_helper(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A direct PR review authenticates its remote fetch without global Git config."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        controlled_env = {"GIT_TERMINAL_PROMPT": "0"}
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}._controlled_git_env", return_value=controlled_env),
            patch(f"{_WP}._trusted_gh_executable", return_value="/opt/homebrew/bin/gh"),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
            patch(f"{_WP}.git_utils.run", return_value=MagicMock(stdout="b" * 40 + "\n")),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"ready": False, "reason": "head_drift"}
        mock_sync.assert_called_once_with(
            tmp_path,
            "70-existing",
            remote="origin",
            pr_number=70,
            timeout=60,
            env=controlled_env,
            fetch_config=(
                "-c",
                (
                    f"core.sshCommand={_executable_path('ssh', path=os.defpath)} "
                    f"-F {os.devnull} -o BatchMode=yes -o StrictHostKeyChecking=yes"
                ),
                "-c",
                "credential.helper=",
                "-c",
                "credential.helper=!/opt/homebrew/bin/gh auth git-credential",
                "-c",
                "core.askPass=",
                "-c",
                "http.sslVerify=true",
            ),
        )

    def test_verify_pr_review_checkout_uses_original_branch_point_when_base_advances(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Review binds to the PR branch point instead of current base-branch HEAD."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "c" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        controlled_env = {"GIT_TERMINAL_PROMPT": "0"}
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch"),
            patch(f"{_WP}._controlled_git_env", return_value=controlled_env),
            patch(f"{_WP}._trusted_gh_executable", return_value="/opt/homebrew/bin/gh"),
            patch(
                f"{_WP}.git_utils.run",
                side_effect=[
                    MagicMock(stdout="a" * 40 + "\n"),
                    MagicMock(stdout=""),
                    MagicMock(stdout="d" * 40 + "\n"),
                    MagicMock(stdout="checkout diff for stale base"),
                    MagicMock(stdout="stale.py\0"),
                ],
            ) as mock_run,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "ready": True,
            "head": "a" * 40,
            "base": "d" * 40,
            "diff": "checkout diff for stale base",
            "changed_paths": ["stale.py"],
        }
        assert mock_run.call_args_list[1].args[0] == [
            "git",
            "-c",
            (
                f"core.sshCommand={_executable_path('ssh', path=os.defpath)} "
                f"-F {os.devnull} -o BatchMode=yes -o StrictHostKeyChecking=yes"
            ),
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!/opt/homebrew/bin/gh auth git-credential",
            "-c",
            "core.askPass=",
            "-c",
            "http.sslVerify=true",
            "fetch",
            "origin",
            "--",
            "main",
        ]
        assert mock_run.call_args_list[1].kwargs["env"] == controlled_env

    def test_verify_pr_review_checkout_returns_diff_bound_to_verified_head(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The review diff comes from the verified checkout, not mutable GitHub output."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as mock_sync,
            patch(
                f"{_WP}.git_utils.run",
                side_effect=[
                    MagicMock(stdout="a" * 40 + "\n"),
                    MagicMock(stdout=""),
                    MagicMock(stdout="b" * 40 + "\n"),
                    MagicMock(stdout="checkout diff for A"),
                    MagicMock(stdout="old.py\0new.py\0"),
                ],
            ) as mock_run,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "ready": True,
            "head": "a" * 40,
            "base": "b" * 40,
            "diff": "checkout diff for A",
            "changed_paths": ["old.py", "new.py"],
        }
        mock_sync.assert_called_once()
        assert mock_sync.call_args.args == (tmp_path, "70-existing")
        assert mock_sync.call_args.kwargs["pr_number"] == 70
        assert mock_sync.call_args.kwargs["timeout"] == 60
        assert mock_run.call_args_list[2].args[0] == [
            "git",
            "merge-base",
            "b" * 40,
            "a" * 40,
        ]
        assert mock_run.call_args_list[3].args[0] == [
            "git",
            "diff",
            "--no-ext-diff",
            "--binary",
            f"{'b' * 40}...{'a' * 40}",
        ]
        assert mock_run.call_args_list[4].args[0] == [
            "git",
            "diff",
            "--no-renames",
            "--name-only",
            "-z",
            f"{'b' * 40}...{'a' * 40}",
        ]

    def test_verify_pr_review_checkout_reports_sync_failure(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A failed sync is never converted into a stale-ready checkout."""
        job = GitJob(
            repo="test/repo",
            op="verify_pr_review_checkout",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path),
                "branch": "70-existing",
                "expected_head_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "base_branch": "main",
                "pr_number": 70,
            },
        )
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True),
            patch(
                f"{_WP}.git_utils.sync_worktree_to_remote_branch",
                side_effect=RuntimeError("remote unavailable"),
            ),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "RuntimeError: remote unavailable"

    def test_create_worktree_defaults_repo_root_to_ambient_cwd(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """No repo_root kwarg falls back to get_repo_root() (single-repo callers)."""
        job = GitJob(
            repo="test/repo",
            op="create_worktree",
            timeout_s=60,
            kwargs={"issue_number": 7, "branch_name": "7-auto"},
        )
        instance = MagicMock()
        ambient_root = get_repo_root()
        instance.create_worktree.return_value = ambient_root / "build" / ".worktrees" / "issue-7"
        with (
            patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager,
            patch(f"{_WP}.get_repo_root", return_value=ambient_root),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(
            base_dir=ambient_root / "build" / ".worktrees",
            repo_root=ambient_root,
        )
        assert result.ok is True

    def test_create_worktree_escaped_repo_root_fails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A worktree path outside the resolved repo_root is a hard failure."""
        repo_root = tmp_path / "Argus"
        job = GitJob(
            repo="Argus",
            op="create_worktree",
            timeout_s=60,
            kwargs={
                "issue_number": 107,
                "branch_name": "107-auto-impl",
                "repo_root": str(repo_root),
            },
        )
        instance = MagicMock()
        instance.create_worktree.return_value = tmp_path / "Hephaestus" / "issue-107"
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error is not None
        assert "escaped resolved repo root" in result.error
        assert str(repo_root) in result.error

    def test_remove_worktree_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """remove_worktree forwards kwargs to WorktreeManager.remove_worktree."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={"issue_number": 7, "force": True},
        )
        instance = MagicMock()
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        instance.remove_worktree.assert_called_once_with(issue_number=7, force=True, timeout=60)
        assert result.ok is True

    def test_remove_worktree_fallback_honors_repo_root_kwarg(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The manager-state fallback (no worktree_path) still scopes repo_root."""
        other_repo = tmp_path / "Argus"
        job = GitJob(
            repo="Argus",
            op="remove_worktree",
            timeout_s=60,
            kwargs={"issue_number": 107, "force": True, "repo_root": str(other_repo)},
        )
        instance = MagicMock()
        with patch(f"{_WP}.WorktreeManager", return_value=instance) as mock_manager:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_manager.assert_called_once_with(repo_root=other_repo)
        assert result.ok is True

    def test_remove_worktree_path_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Path cleanup removes the known worktree path even with a fresh manager."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-7"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_head": "a" * 40,
                "expected_detached": True,
                "force": False,
            },
        )
        records = [{"path": str(tmp_path / "issue-7"), "commit": "a" * 40}]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            mock_run.return_value.stdout = ""
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_any_call(
            ["git", "worktree", "remove", str(tmp_path / "issue-7")],
            cwd=tmp_path,
            timeout=60,
        )
        mock_run.assert_any_call(
            ["git", "worktree", "prune"],
            cwd=tmp_path,
            check=False,
            timeout=60,
        )
        assert result.ok is True

    def test_remove_worktree_rejects_dirty_checkout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Cleanup preserves tracked and untracked work after learning."""
        worktree = tmp_path / "issue-7"
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(worktree),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_head": "a" * 40,
                "expected_detached": True,
                "force": False,
            },
        )
        records = [{"path": str(worktree), "commit": "a" * 40}]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            mock_run.return_value.stdout = "?? learning-notes.md\n"
            pool.submit(job, StageName.FINISHED)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup refused a dirty checkout"
        assert not any(
            call.args and call.args[0][:3] == ["git", "worktree", "remove"]
            for call in mock_run.call_args_list
        )

    def test_remove_worktree_requires_ownership_proof(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Path cleanup cannot remove a checkout without a bound branch or head."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "review-pr-7"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
            },
        )
        with patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run:
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup identity is invalid"
        mock_run.assert_not_called()

    def test_remove_generated_detached_review_worktree(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A positive numeric review generation is a valid managed path."""
        worktree = tmp_path / "review-pr-7-2"
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(worktree),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_head": "a" * 40,
                "expected_detached": True,
            },
        )
        records = [{"path": str(worktree), "commit": "a" * 40}]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            mock_run.return_value.stdout = ""
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        mock_run.assert_any_call(
            ["git", "worktree", "remove", str(worktree)],
            cwd=tmp_path,
            timeout=60,
        )

    def test_remove_detached_review_rejects_branch_attachment(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Detached cleanup preserves a replacement attached to a branch."""
        worktree = tmp_path / "review-pr-7-1"
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(worktree),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_head": "a" * 40,
                "expected_detached": True,
            },
        )
        records = [
            {
                "path": str(worktree),
                "commit": "a" * 40,
                "branch": "refs/heads/human-work",
            }
        ]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup ownership changed"
        mock_run.assert_not_called()

    @pytest.mark.parametrize("name", ["review-pr-7-0", "review-pr-7-next", "review-pr-7--1"])
    def test_remove_review_worktree_rejects_invalid_generation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        name: str,
    ) -> None:
        """Only a positive numeric review generation is admitted."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / name),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_head": "a" * 40,
                "expected_detached": True,
            },
        )
        with patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run:
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup identity is invalid"
        mock_run.assert_not_called()

    def test_remove_worktree_rejects_path_outside_issue_identity(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Recovered cleanup cannot remove a path for another identity."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-8"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "force": True,
            },
        )
        with patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup identity is invalid"
        mock_run.assert_not_called()

    def test_remove_worktree_rejects_replacement_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Recovered cleanup cannot remove a replacement worktree at the expected path."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-7"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_branch": "7-auto",
                "force": True,
            },
        )
        records = [
            {
                "path": str(tmp_path / "issue-7"),
                "branch": "refs/heads/human-work",
                "commit": "a" * 40,
            }
        ]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup ownership changed"
        mock_run.assert_not_called()

    def test_remove_worktree_rejects_replacement_head(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Delayed cleanup cannot remove a checkout that moved to a new commit."""
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-7"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "expected_branch": "7-auto",
                "expected_head": "a" * 40,
                "force": False,
            },
        )
        records = [
            {
                "path": str(tmp_path / "issue-7"),
                "branch": "refs/heads/7-auto",
                "commit": "b" * 40,
            }
        ]
        with (
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch("hephaestus.automation.pipeline.git_cleanup.run") as mock_run,
        ):
            pool.submit(job, StageName.FINISHED)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "worktree cleanup ownership changed"
        mock_run.assert_not_called()

    def test_remove_worktree_conditionally_releases_noop_local_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The branch delete runs only after its worktree is removed and pruned."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="remove_worktree",
            timeout_s=60,
            kwargs={
                "worktree_path": str(tmp_path / "issue-7"),
                "repo_root": str(tmp_path),
                "issue_number": 7,
                "force": True,
                "expected_branch": "7-auto",
                "local_branch_cleanup": {"branch": "7-auto", "base_sha": pin},
            },
        )
        records = [
            {
                "path": str(tmp_path / "issue-7"),
                "branch": "refs/heads/7-auto",
                "commit": pin,
            }
        ]
        with (
            patch("hephaestus.automation.pipeline.git_cleanup.run") as run,
            patch(
                "hephaestus.automation.pipeline.git_cleanup.WorktreeManager.list_worktrees",
                return_value=records,
            ),
            patch(
                "hephaestus.automation.pipeline.git_cleanup.delete_local_branch_if_unchanged",
                return_value=True,
            ) as release,
        ):
            run.return_value.stdout = ""
            pool.submit(job, StageName.FINISHED)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"local_branch_deleted": True}
        release.assert_called_once_with("7-auto", pin, tmp_path, timeout=60)

    @pytest.mark.parametrize(
        ("rebase_clean", "expected_error"),
        [
            (True, None),
            (False, "mechanical rebase hit conflicts; aborted"),
        ],
    )
    def test_rebase_dispatch_propagates_result(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        rebase_clean: bool,
        expected_error: str | None,
    ) -> None:
        """Rebase propagates its status and explains an aborted conflict."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={"cwd": Path("/tmp/wt"), "base_branch": "main"},
        )
        with (
            patch(
                "hephaestus.automation.git_utils.rebase_worktree_onto",
                return_value=rebase_clean,
            ) as mock_rebase,
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(
                    {"GIT_TERMINAL_PROMPT": "0"},
                    ("-c", "credential.helper=!trusted-gh auth git-credential"),
                ),
            ),
            patch(
                f"{_WP}._controlled_git_signing_env",
                return_value={"GIT_CONFIG_KEY_0": "user.signingkey"},
            ),
        ):
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        mock_rebase.assert_called_once_with(
            cwd=Path("/tmp/wt"),
            base_branch="main",
            preserve_conflicts=False,
            timeout=60,
            env={"GIT_CONFIG_KEY_0": "user.signingkey"},
            fetch_env={"GIT_TERMINAL_PROMPT": "0"},
            fetch_config=("-c", "credential.helper=!trusted-gh auth git-credential"),
        )
        assert result.ok is rebase_clean
        if rebase_clean:
            assert result.value is True
        else:
            assert result.value is False
        assert result.error == expected_error

    def test_remote_git_configuration_preserves_hooks_and_isolates_ssh(
        self,
        pool: WorkerPool,
    ) -> None:
        """Authenticated remote Git keeps hooks active and pins the SSH client."""
        with (
            patch(f"{_WP}._controlled_git_env", return_value={"GIT_TERMINAL_PROMPT": "0"}),
            patch(f"{_WP}._trusted_gh_executable", return_value="/opt/homebrew/bin/gh"),
            patch(f"{_WP}._trusted_executable", return_value="/usr/bin/ssh"),
        ):
            env, remote_config = pool._authenticated_remote_git_configuration()

        assert env == {"GIT_TERMINAL_PROMPT": "0"}
        assert "core.hooksPath=/dev/null" not in remote_config
        assert (
            "core.sshCommand=/usr/bin/ssh -F /dev/null "
            "-o BatchMode=yes -o StrictHostKeyChecking=yes"
        ) in remote_config
        assert "credential.helper=!/opt/homebrew/bin/gh auth git-credential" in remote_config

    def test_remote_git_configuration_rejects_changed_origin_before_push(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A writer cannot redirect a host-authenticated remote mutation."""
        (tmp_path / ".git").mkdir()
        with (
            patch(f"{_WP}._checkout_preflight_error", return_value=None),
            patch(
                f"{_WP}.git_utils.run",
                return_value=MagicMock(stdout="https://github.com/attacker/target.git\n"),
            ),
            pytest.raises(RuntimeError, match="unexpected origin"),
        ):
            pool._authenticated_remote_git_configuration(
                cwd=tmp_path,
                expected_repo="owner/name",
                timeout=60,
            )

    def test_worktree_sync_binds_authentication_to_expected_repository(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """Remote synchronization validates its destination before the fetch."""
        remote_env = {"GIT_TERMINAL_PROMPT": "0"}
        remote_config = ("-c", "credential.helper=!trusted-gh auth git-credential")
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ) as authentication,
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as sync,
        ):
            pool._sync_worktree_to_remote_branch(
                tmp_path,
                "topic",
                expected_repo="owner/name",
                timeout=45,
            )

        authentication.assert_called_once_with(
            cwd=tmp_path,
            expected_repo="owner/name",
            timeout=45,
        )
        sync.assert_called_once_with(
            tmp_path,
            "topic",
            remote="origin",
            pr_number=None,
            timeout=45,
            env=remote_env,
            fetch_config=remote_config,
        )

    def test_rebase_fails_closed_without_authenticated_remote_helper(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A remote rebase stops before Git runs when the GitHub helper is absent."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={"cwd": tmp_path, "base_branch": "main"},
        )
        with (
            patch(f"{_WP}._trusted_gh_executable", return_value=None),
            patch(f"{_WP}.git_utils.rebase_worktree_onto") as rebase,
        ):
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "required GitHub executable is unavailable"
        assert result.value == {"failure_kind": "remote_authentication"}
        rebase.assert_not_called()

    def test_writer_publish_rebase_conflict_returns_actionable_reason(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The active writer publish path preserves the conflict explanation."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "publish_rebased_head": True,
                "branch": "7-auto-impl",
                "expected_remote_sha": "a" * 40,
            },
        )
        with (
            patch(
                "hephaestus.automation.git_utils.rebase_worktree_onto",
                return_value=False,
            ),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.run") as run,
            patch.object(pool, "_conflict_receipt") as receipt,
        ):
            run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=1),
            ]
            receipt.return_value = {
                "rebased": False,
                "conflict_paths": ("x.py",),
                "conflict_snapshot": {"x.py": "before"},
                "conflict_index_snapshot": "1" * 64,
                "paused_head_sha": "c" * 40,
                "base_sha": "b" * 40,
                "expected_remote_sha": "a" * 40,
            }
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == receipt.return_value
        assert result.error == "mechanical rebase hit conflicts; resolution required"

    def test_clean_rebase_revalidates_destination_after_commit_hooks(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A successful rebase gets a fresh destination proof before publication."""
        job = GitJob(
            repo="owner/name",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "publish_rebased_head": True,
                "branch": "7-auto-impl",
                "expected_remote_sha": "a" * 40,
            },
        )
        first_env = {"AUTH": "before-hooks"}
        fresh_env = {"AUTH": "after-hooks"}
        first_config = ("-c", "credential.helper=!first")
        fresh_config = ("-c", "credential.helper=!fresh")

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=1)
            return MagicMock(returncode=0, stdout="")

        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                side_effect=((first_env, first_config), (fresh_env, fresh_config)),
            ) as authentication,
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
            patch(f"{_WP}.git_utils.rebase_worktree_onto", return_value=True),
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            result = pool._git_rebase(job)

        assert result.ok is True
        assert authentication.call_args_list == [
            call(cwd=tmp_path, expected_repo="owner/name", timeout=60),
            call(cwd=tmp_path, expected_repo="owner/name", timeout=60),
        ]
        push.assert_called_once_with(
            "7-auto-impl",
            "a" * 40,
            tmp_path,
            source_sha="b" * 40,
            timeout=60,
            env=fresh_env,
            remote_config=fresh_config,
            revalidate_remote=ANY,
        )

    def test_remote_head_probe_binds_expected_repository(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A post-agent remote probe validates its checkout destination."""
        remote_env = {"GIT_TERMINAL_PROMPT": "0"}
        remote_config = ("-c", "credential.helper=!trusted")
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ) as authentication,
            patch(
                f"{_WP}.git_utils.run",
                return_value=MagicMock(stdout=f"{'a' * 40} refs/heads/topic\n"),
            ),
        ):
            result = pool._read_remote_branch_head(
                tmp_path,
                remote="origin",
                branch="topic",
                expected_repo="owner/name",
                timeout=60,
            )

        assert result == "a" * 40
        authentication.assert_called_once_with(
            cwd=tmp_path,
            expected_repo="owner/name",
            timeout=60,
        )

    def test_dependency_sync_aborts_conflict_without_a_resolution_receipt(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A child-dependency sync never leaves a rebase for an agent."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "publish_rebased_head": True,
                "abort_on_conflict": True,
                "required_ancestor_shas": ("b" * 40,),
                "branch": "7-auto-impl",
                "expected_remote_sha": "a" * 40,
            },
        )
        with (
            patch(
                "hephaestus.automation.git_utils.rebase_worktree_onto",
                return_value=False,
            ) as rebase,
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.run") as run,
            patch.object(pool, "_conflict_receipt") as receipt,
        ):
            run.side_effect = [MagicMock(returncode=0), MagicMock(returncode=1)]
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        rebase.assert_called_once_with(
            cwd=tmp_path,
            base_branch="main",
            preserve_conflicts=False,
            timeout=60,
            env=ANY,
            fetch_env=ANY,
            fetch_config=ANY,
        )
        receipt.assert_not_called()
        assert result.ok is False
        assert result.error == "mechanical rebase hit conflicts; aborted"

    def test_conflict_receipt_binds_index_head_base_and_remote_head(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """The host captures the complete index before agent file editing."""
        (tmp_path / "x.py").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> topic\n")
        index_state = (
            "100644 host-blob 0\thost-staged.py\0"
            "100644 ours-blob 1\tx.py\0"
            "100644 theirs-blob 2\tx.py\0"
        )
        with patch(f"{_WP}.git_utils.run") as run:
            run.side_effect = [
                MagicMock(returncode=0, stdout="x.py\0"),
                MagicMock(returncode=0, stdout=index_state),
                MagicMock(returncode=0, stdout="c" * 40),
                MagicMock(returncode=0, stdout="b" * 40),
            ]

            receipt = pool._conflict_receipt(
                tmp_path,
                remote="origin",
                base_branch="main",
                expected_remote_sha="a" * 40,
                timeout=60,
            )

        assert isinstance(receipt, dict)
        assert receipt["conflict_paths"] == ("x.py",)
        assert (
            receipt["conflict_index_snapshot"] == hashlib.sha256(index_state.encode()).hexdigest()
        )
        assert receipt["paused_head_sha"] == "c" * 40
        assert receipt["base_sha"] == "b" * 40
        assert receipt["expected_remote_sha"] == "a" * 40
        assert run.call_args_list[1].args[0] == [
            "git",
            "ls-files",
            "--stage",
            "-z",
        ]

    @staticmethod
    def _continue_rebase_job(tmp_path: Path, *, repo: str = "Hephaestus") -> GitJob:
        return GitJob(
            repo=repo,
            op="continue_rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "remote": "origin",
                "branch": "7-auto-impl",
                "base_sha": "b" * 40,
                "expected_remote_sha": "a" * 40,
                "conflict_paths": ("x.py",),
                "conflict_snapshot": {"x.py": "before"},
                "conflict_index_snapshot": "1" * 64,
                "paused_head_sha": "c" * 40,
            },
        )

    def test_continue_rebase_rejects_noop_agent(self, pool: WorkerPool, tmp_path: Path) -> None:
        """An unchanged conflict snapshot cannot advance to Git continuation."""
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "before"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch(f"{_WP}.git_utils.run") as run,
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "rebase conflict resolution required: agent made no file changes"
        run.assert_not_called()

    def test_continue_rebase_selected_policy_semantic_failure_does_not_publish(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Semantic validation fails closed after Git completes and before push."""
        (tmp_path / "x.py").write_text("resolved\n")
        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "0027-durable-plan-review-conversations.md").write_text("# plan\n")
        (adr_dir / "0027-host-owned-learning-preparation.md").write_text("# learning\n")
        structural_test = tmp_path / "tests" / "unit" / "docs" / "test_adr_records.py"
        structural_test.parent.mkdir(parents=True)
        structural_test.write_text("# structural test\n", encoding="utf-8")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv == ["git", "diff", "--name-only", "-z"]:
                return MagicMock(returncode=0, stdout="x.py\0")
            return MagicMock(returncode=0, stdout="")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch.object(pool, "_run_immutable_build_test", return_value=JobResult(ok=True)),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.value == {
            "failure_kind": "semantic_validation",
            "rebase_policy": "hephaestus-adr-v1",
        }
        assert result.error == (
            "rebase policy hephaestus-adr-v1 semantic validation failed: "
            "rebase semantic validation failed: duplicate ADR number 0027 "
            "(0027-durable-plan-review-conversations.md, "
            "0027-host-owned-learning-preparation.md)"
        )
        push.assert_not_called()

    def test_continue_rebase_selected_policy_structural_failure_does_not_publish(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A selected structural failure stops publication and keeps diagnostics."""
        (tmp_path / "x.py").write_text("resolved\n")
        test_path = tmp_path / "tests" / "unit" / "docs" / "test_adr_records.py"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("# repository-owned structural test\n")
        failed = JobResult(
            ok=False,
            value={"failure_kind": "validation"},
            error="rc=1",
            stdout_tail="duplicate ADR number 0027",
            stderr_tail="pytest diagnostics",
        )
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv == ["git", "diff", "--name-only", "-z"]:
                return MagicMock(returncode=0, stdout="x.py\0")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=0, stdout="")
            if argv[:3] == ["git", "rev-list", "--reverse"]:
                return MagicMock(returncode=0, stdout="c" * 40)
            if argv[:3] == ["git", "cat-file", "-p"]:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                        "Signed-off-by: Test User <test@example.com>\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch.object(pool, "_run_immutable_build_test", return_value=failed),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result == JobResult(
            ok=False,
            value={"failure_kind": "validation", "rebase_policy": "hephaestus-adr-v1"},
            error="rebase policy hephaestus-adr-v1 structural validation failed: rc=1",
            stdout_tail="duplicate ADR number 0027",
            stderr_tail="pytest diagnostics",
        )
        push.assert_not_called()

    def test_continue_rebase_unconfigured_target_adr_layout_publishes(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """An unconfigured target publishes after a valid conflict continuation."""
        (tmp_path / "x.py").write_text("resolved\n")
        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "index.md").write_text("# Index\n", encoding="utf-8")
        (adr_dir / "0000-template.md").write_text("# Template\n", encoding="utf-8")
        (adr_dir / "0001-fleet-routing.md").write_text("# Fleet routing\n", encoding="utf-8")
        job = self._continue_rebase_job(tmp_path, repo="Comet")
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv == ["git", "diff", "--name-only", "-z"]:
                return MagicMock(returncode=0, stdout="x.py\0")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=0, stdout="")
            if argv[:3] == ["git", "rev-list", "--reverse"]:
                return MagicMock(returncode=0, stdout="c" * 40)
            if argv[:3] == ["git", "cat-file", "-p"]:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                        "Signed-off-by: Test User <test@example.com>\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch.object(
                pool,
                "_run_rebase_structural_validation",
                wraps=pool._run_rebase_structural_validation,
            ) as structural,
            patch.object(
                pool, "_validate_rebased_tree", wraps=pool._validate_rebased_tree
            ) as semantic,
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result == JobResult(
            ok=True,
            value={
                "rebased": True,
                "published": True,
                "head_sha": "d" * 40,
                "rebase_policy": None,
            },
        )
        structural.assert_called_once_with(tmp_path, timeout=60, policy=None)
        semantic.assert_called_once_with(tmp_path, policy=None)
        push.assert_called_once()

    def test_rebase_structural_validation_preserves_bounded_diagnostics(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A failing repository test returns its bounded output without publishing."""
        test_path = tmp_path / "tests" / "unit" / "docs" / "test_adr_records.py"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("# repository-owned structural test\n")
        failed = JobResult(
            ok=False,
            value={"failure_kind": "validation"},
            error="rc=1",
            stdout_tail="duplicate ADR number 0027",
            stderr_tail="pytest diagnostics",
        )

        with (
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch.object(pool, "_run_immutable_build_test", return_value=failed) as run_test,
        ):
            result = pool._run_rebase_structural_validation(
                tmp_path,
                timeout=60,
                policy=pool._select_rebase_policy("Hephaestus"),
            )

        assert result == JobResult(
            ok=False,
            value={"failure_kind": "validation", "rebase_policy": "hephaestus-adr-v1"},
            error="rebase policy hephaestus-adr-v1 structural validation failed: rc=1",
            stdout_tail="duplicate ADR number 0027",
            stderr_tail="pytest diagnostics",
        )
        run_test.assert_called_once()

    def test_continue_rebase_rejects_unresolved_markers(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Edited content still carrying conflict markers remains paused."""
        (tmp_path / "x.py").write_text("<<<<<<< HEAD\na\n=======\nb\n>>>>>>> topic\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "rebase conflict resolution required: conflict markers remain"

    def test_continue_rebase_rejects_edits_outside_conflict_paths(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """The host rejects an agent turn that dirtied any non-conflict path."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv == ["git", "diff", "--name-only", "-z"]:
                return MagicMock(returncode=0, stdout="x.py\0outside.py\0")
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=0, stdout="")
            if argv[:3] == ["git", "rev-list", "--reverse"]:
                return MagicMock(returncode=0, stdout="c" * 40)
            if argv[:3] == ["git", "cat-file", "-p"]:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                        "Signed-off-by: Test User <test@example.com>\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch"),
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "rebase conflict resolution changed paths outside host scope"

    def test_rebase_conflict_scope_allows_host_staged_nonconflict_paths(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Clean paths staged by the paused rebase are host state, not agent edits."""

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv == ["git", "diff", "--name-only", "-z"]:
                return MagicMock(returncode=0, stdout="conflict.py\0")
            if argv == ["git", "diff", "--cached", "--name-only", "-z"]:
                return MagicMock(
                    returncode=0,
                    stdout="conflict.py\0host-staged.py\0",
                )
            if argv == ["git", "ls-files", "--others", "--exclude-standard", "-z"]:
                return MagicMock(returncode=0, stdout="")
            raise AssertionError(f"unexpected git probe: {argv!r}")

        with patch(f"{_WP}.git_utils.run", side_effect=fake_run):
            result = pool._rebase_conflict_edit_scope_error(
                tmp_path,
                conflict_paths=("conflict.py",),
                timeout=1,
            )

        assert result is None

    def test_continue_rebase_rejects_mutated_conflict_index(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Only the host may mutate the paused rebase index."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "2" * 64,
            "paused_head_sha": "c" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=0, stdout="")
            if argv[:3] == ["git", "rev-list", "--reverse"]:
                return MagicMock(returncode=0, stdout="c" * 40)
            if argv[:3] == ["git", "cat-file", "-p"]:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                        "Signed-off-by: Test User <test@example.com>\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="x.py\0")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch"),
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "conflict index was mutated outside host ownership"

    def test_continue_rebase_rejects_agent_staged_nonconflict_path(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A stage-0 mutation outside the conflict paths cannot enter continuation."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            # The complete index changed after the agent staged outside.py.
            "conflict_index_snapshot": "2" * 64,
            "paused_head_sha": "c" * 40,
        }
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch(f"{_WP}.git_utils.run") as run,
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "conflict index was mutated outside host ownership"
        run.assert_not_called()

    def test_continue_rebase_rejects_changed_paused_head(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """Agent file editing cannot move the host-owned paused rebase head."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "d" * 40,
        }

        def fake_run(argv: list[str], **_kwargs: object) -> MagicMock:
            if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
                return MagicMock(returncode=0, stdout="")
            if argv[:3] == ["git", "rev-list", "--reverse"]:
                return MagicMock(returncode=0, stdout="e" * 40)
            if argv[:3] == ["git", "cat-file", "-p"]:
                return MagicMock(
                    returncode=0,
                    stdout=(
                        "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                        "Signed-off-by: Test User <test@example.com>\n"
                    ),
                )
            return MagicMock(returncode=0, stdout="x.py\0")

        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_read_publish_head", return_value="f" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch"),
            patch(f"{_WP}.git_utils.run", side_effect=fake_run),
        ):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "paused rebase head changed outside host ownership"

    def test_continue_rebase_rejects_remote_head_drift(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """The captured PR head is an exact publication lease, not a hint."""
        job = self._continue_rebase_job(tmp_path)
        with patch.object(pool, "_read_remote_branch_head", return_value="c" * 40):
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "remote writer head changed during conflict resolution"

    def test_continue_rebase_reports_signing_failure_diagnostics(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A signing failure is distinct from a follow-up content conflict."""
        failure = subprocess.CalledProcessError(
            128,
            ["git", "rebase", "--continue"],
            output="rebase output",
            stderr="error: cannot run gpg: No such file or directory",
        )
        with (
            patch(f"{_WP}._controlled_git_signing_env", return_value={"GIT_EDITOR": "true"}),
            patch(
                f"{_WP}.git_utils.run",
                side_effect=[MagicMock(), MagicMock(), failure],
            ),
            patch.object(
                pool,
                "_conflict_receipt",
                return_value=JobResult(
                    ok=False,
                    error="paused rebase conflict paths invalid",
                ),
            ),
        ):
            result = pool._continue_rebase_process(
                tmp_path,
                remote="origin",
                base_sha="b" * 40,
                expected_remote_sha="a" * 40,
                paths=("x.py",),
                timeout=60,
            )

        assert result is not None and result.ok is False
        assert result.error == "host rebase continuation signing failed"
        assert result.value == {
            "failure_kind": "signing",
            "phase": "rebase_continue",
            "returncode": 128,
            "receipt_error": "paused rebase conflict paths invalid",
        }
        assert result.stdout_tail == "rebase output"
        assert "cannot run gpg" in result.stderr_tail

    def test_continue_rebase_rejects_missing_captured_base_ancestry(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A host-completed rebase must descend from its captured base head."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_run_rebase_structural_validation", return_value=None),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.run") as run,
        ):
            run.side_effect = [
                MagicMock(returncode=0, stdout="x.py\0"),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=1, stdout=""),
            ]
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "completed rebase lacks captured base ancestry"

    @pytest.mark.parametrize(
        "raw_commit",
        [
            "tree deadbeef\n\nmessage\n\nSigned-off-by: Test User <test@example.com>\n",
            "tree deadbeef\ngpgsig signature\n\nmessage\n",
        ],
    )
    def test_continue_rebase_rejects_unsigned_or_non_dco_commit(
        self, pool: WorkerPool, tmp_path: Path, raw_commit: str
    ) -> None:
        """Host completion verifies every replayed commit's signature and DCO trailer."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_run_rebase_structural_validation", return_value=None),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch(f"{_WP}.git_utils.run") as run,
        ):
            run.side_effect = [
                MagicMock(returncode=0, stdout="x.py\0"),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout="c" * 40),
                MagicMock(returncode=0, stdout=raw_commit),
            ]
            result = pool._git_continue_rebase(job)

        assert result.ok is False
        assert result.error == "completed rebase commit metadata invalid"

    def test_continue_rebase_signs_verifies_and_exact_lease_publishes(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A valid resolution advances only through the host's exact-head publication."""
        (tmp_path / "x.py").write_text("resolved\n")
        job = self._continue_rebase_job(tmp_path)
        receipt = {
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "after"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        }
        signed = (
            "tree deadbeef\ngpgsig -----BEGIN SIGNATURE-----\n\nfix\n\n"
            "Signed-off-by: Micah Villmow "
            "<4211002+mvillmow@users.noreply.github.com>\n"
        )
        with (
            patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
            patch.object(pool, "_conflict_receipt", return_value=receipt),
            patch.object(pool, "_run_rebase_structural_validation", return_value=None),
            patch(f"{_WP}._controlled_git_signing_env", return_value={}),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(
                    {"GIT_TERMINAL_PROMPT": "0"},
                    ("-c", "credential.helper=!trusted-gh auth git-credential"),
                ),
            ),
            patch.object(pool, "_read_publish_head", return_value="d" * 40),
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
            patch(f"{_WP}.git_utils.run") as run,
        ):
            run.side_effect = [
                MagicMock(returncode=0, stdout="x.py\0"),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout=""),
                MagicMock(returncode=0, stdout="c" * 40),
                MagicMock(returncode=0, stdout=signed),
            ]
            result = pool._git_continue_rebase(job)

        assert result == JobResult(
            ok=True,
            value={
                "rebased": True,
                "published": True,
                "head_sha": "d" * 40,
                "rebase_policy": "hephaestus-adr-v1",
            },
        )
        push.assert_called_once_with(
            "7-auto-impl",
            "a" * 40,
            tmp_path,
            source_sha="d" * 40,
            timeout=60,
            env={"GIT_TERMINAL_PROMPT": "0"},
            remote_config=("-c", "credential.helper=!trusted-gh auth git-credential"),
            revalidate_remote=ANY,
        )

    @pytest.mark.usefixtures("require_git_path_format")
    def test_continue_rebase_recovers_two_real_conflicts_and_publishes(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """Sequential real conflicts each yield a receipt before exact publication."""
        origin = tmp_path / "origin.git"
        checkout = tmp_path / "checkout"
        signing_key = tmp_path / "signing-key"
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(origin)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "init", "--initial-branch", "main", str(checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                _executable_path("ssh-keygen"),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                str(signing_key),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (
            ("user.name", "Test User"),
            ("user.email", "test@example.invalid"),
            ("gpg.format", "ssh"),
            ("user.signingkey", str(signing_key)),
            ("commit.gpgsign", "false"),
        ):
            subprocess.run(
                ["git", "config", key, value],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "remote", "add", "origin", str(origin)],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )

        (checkout / "a.txt").write_text("base-a\n", encoding="utf-8")
        (checkout / "b.txt").write_text("base-b\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "a.txt", "b.txt"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "test: add base files"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "main"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "switch", "-c", "7-auto-impl"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        (checkout / "a.txt").write_text("topic-a\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "fix: change a"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        (checkout / "b.txt").write_text("topic-b\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "fix: change b"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "-u", "origin", "7-auto-impl"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        expected_remote_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        subprocess.run(
            ["git", "switch", "main"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        (checkout / "a.txt").write_text("main-a\n", encoding="utf-8")
        (checkout / "b.txt").write_text("main-b\n", encoding="utf-8")
        subprocess.run(
            ["git", "commit", "-am", "test: change base files"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "push", "origin", "main"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "switch", "7-auto-impl"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )

        signing = {
            "user.name": "Test User",
            "user.email": "test@example.invalid",
            "gpg.format": "ssh",
            "user.signingkey": str(signing_key),
        }
        rebase_job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": checkout,
                "base_branch": "main",
                "remote": "origin",
                "publish_rebased_head": True,
                "branch": "7-auto-impl",
                "expected_remote_sha": expected_remote_sha,
            },
        )

        with (
            patch(f"{_WP}._read_host_git_signing_config", return_value=signing),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(os.environ.copy(), ()),
            ),
        ):
            first = pool._git_rebase(rebase_job)
            assert first.ok is False
            assert first.error == "mechanical rebase hit conflicts; resolution required"
            assert isinstance(first.value, dict)
            assert first.value["conflict_paths"] == ("a.txt",)
            assert first.value["base_sha"] == base_sha

            (checkout / "a.txt").write_text("resolved-a\n", encoding="utf-8")
            first_continuation = GitJob(
                repo="test/repo",
                op="continue_rebase",
                timeout_s=60,
                kwargs={
                    "cwd": checkout,
                    "remote": "origin",
                    "branch": "7-auto-impl",
                    **{key: value for key, value in first.value.items() if key != "rebased"},
                },
            )
            second = pool._git_continue_rebase(first_continuation)
            assert second.ok is False
            assert second.error == (
                "rebase conflict resolution required: additional conflicts found"
            )
            assert isinstance(second.value, dict)
            assert second.value["conflict_paths"] == ("b.txt",)
            assert second.value["base_sha"] == base_sha

            (checkout / "b.txt").write_text("resolved-b\n", encoding="utf-8")
            second_continuation = GitJob(
                repo="test/repo",
                op="continue_rebase",
                timeout_s=60,
                kwargs={
                    "cwd": checkout,
                    "remote": "origin",
                    "branch": "7-auto-impl",
                    **{key: value for key, value in second.value.items() if key != "rebased"},
                },
            )
            completed = pool._git_continue_rebase(second_continuation)

        assert completed.ok is True
        assert isinstance(completed.value, dict)
        assert completed.value["rebased"] is True
        assert completed.value["published"] is True
        published_sha = str(completed.value["head_sha"])
        remote_sha = subprocess.run(
            ["git", "ls-remote", "origin", "refs/heads/7-auto-impl"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        assert remote_sha == published_sha
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_sha, published_sha],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        for commit in subprocess.run(
            ["git", "rev-list", f"{base_sha}..{published_sha}"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split():
            raw_commit = subprocess.run(
                ["git", "cat-file", "-p", commit],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert "\ngpgsig " in f"\n{raw_commit}"
            assert "Signed-off-by: Test User <test@example.invalid>" in raw_commit

    def test_writer_rebase_keeps_exact_head_when_current_base_is_already_ancestor(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Review preparation must not rewrite or publish an already-current PR."""
        head = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "remote": "origin",
                "publish_rebased_head": True,
                "branch": "7-auto-impl",
                "expected_remote_sha": head,
            },
        )
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(
                    {"GIT_TERMINAL_PROMPT": "0"},
                    ("-c", "credential.helper=!trusted-gh auth git-credential"),
                ),
            ),
            patch(f"{_WP}.git_utils.run") as run,
            patch(f"{_WP}.git_utils.rebase_worktree_onto") as rebase,
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=f"{head}\n"),
                MagicMock(
                    returncode=0,
                    stdout=f"{head}\trefs/heads/7-auto-impl\n",
                ),
            ]
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "rebased": False,
            "published": False,
            "head_sha": head,
        }
        assert [call.args[0] for call in run.call_args_list] == [
            [
                "git",
                "-c",
                "credential.helper=!trusted-gh auth git-credential",
                "fetch",
                "origin",
                "main",
            ],
            ["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
            ["git", "rev-parse", "HEAD"],
            [
                "git",
                "-c",
                "credential.helper=!trusted-gh auth git-credential",
                "ls-remote",
                "--refs",
                "origin",
                "refs/heads/7-auto-impl",
            ],
        ]
        rebase.assert_not_called()
        push.assert_not_called()
        assert run.call_args_list[-1].kwargs["env"] == {"GIT_TERMINAL_PROMPT": "0"}

    def test_writer_rebase_rejects_noop_when_remote_branch_moves(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Review preparation must reject a stale head after a concurrent push."""
        expected_head = "a" * 40
        moved_head = "b" * 40
        branch = "7-auto-impl"
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "remote": "origin",
                "publish_rebased_head": True,
                "branch": branch,
                "expected_remote_sha": expected_head,
            },
        )
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(
                    {"GIT_TERMINAL_PROMPT": "0"},
                    ("-c", "credential.helper=!trusted-gh auth git-credential"),
                ),
            ),
            patch(f"{_WP}.git_utils.run") as run,
            patch(f"{_WP}.git_utils.rebase_worktree_onto") as rebase,
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            run.side_effect = [
                MagicMock(returncode=0),
                MagicMock(returncode=0),
                MagicMock(returncode=0, stdout=f"{expected_head}\n"),
                MagicMock(
                    returncode=0,
                    stdout=f"{moved_head}\trefs/heads/{branch}\n",
                ),
            ]
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "remote writer head changed during rebase preparation"
        assert run.call_args_list[-1] == call(
            [
                "git",
                "-c",
                "credential.helper=!trusted-gh auth git-credential",
                "ls-remote",
                "--refs",
                "origin",
                f"refs/heads/{branch}",
            ],
            cwd=tmp_path,
            timeout=60,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        rebase.assert_not_called()
        push.assert_not_called()

    def test_restored_writer_publish_rebase_syncs_and_returns_to_review_on_head_drift(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A restored writer must not rebase stale local H against newer remote E."""
        expected_head = "a" * 40
        synced_head = "b" * 40
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "remote": "origin",
                "publish_rebased_head": True,
                "branch": "7-auto-impl",
                "expected_remote_sha": expected_head,
                "sync_to_expected_remote_head": True,
                "pr_number": 70,
            },
        )
        with (
            patch(f"{_WP}.git_utils.is_clean_working_tree", return_value=True) as clean,
            patch(f"{_WP}.git_utils.sync_worktree_to_remote_branch") as sync,
            patch.object(pool, "_read_publish_head", return_value=synced_head),
            patch(f"{_WP}.git_utils.run") as run,
            patch(f"{_WP}.git_utils.rebase_worktree_onto") as rebase,
            patch(f"{_WP}.git_utils.push_head_to_branch") as push,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {
            "rebased": False,
            "published": False,
            "head_drift": True,
            "head_sha": synced_head,
        }
        clean.assert_called_once_with(tmp_path, timeout=60)
        sync.assert_called_once()
        assert sync.call_args.args == (tmp_path, "7-auto-impl")
        assert sync.call_args.kwargs["remote"] == "origin"
        assert sync.call_args.kwargs["pr_number"] == 70
        assert sync.call_args.kwargs["timeout"] == 60
        run.assert_not_called()
        rebase.assert_not_called()
        push.assert_not_called()

    def test_direct_rebase_dispatch_rejects_the_retired_publish_mode(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The reviewer-only detached publish mode has no compatibility path."""
        job = GitJob(
            repo="test/repo",
            op="rebase",
            timeout_s=60,
            kwargs={
                "cwd": tmp_path,
                "base_branch": "main",
                "branch": "70-existing",
                "expected_remote_sha": "a" * 40,
                "publish_detached_head": True,
            },
        )
        with patch(f"{_WP}.git_utils.rebase_worktree_onto") as mock_rebase:
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_rebase.assert_not_called()
        assert result.ok is False
        assert result.error == "detached reviewer rebase publication is unsupported"

    def test_push_dispatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Push forwards to push_current_branch_with_lease_on_divergence."""
        job = GitJob(
            repo="test/repo",
            op="push",
            timeout_s=60,
            kwargs={"cwd": Path("/tmp/wt"), "branch": "7-auto"},
        )
        with patch(
            "hephaestus.automation.git_utils.push_current_branch_with_lease_on_divergence"
        ) as mock_push:
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        mock_push.assert_called_once_with(
            cwd=Path("/tmp/wt"),
            branch="7-auto",
            timeout=60,
            env=ANY,
            remote_config=ANY,
            revalidate_remote=ANY,
        )
        assert result.ok is True

    @pytest.mark.parametrize(
        ("push_error", "failure_kind", "error"),
        [
            (
                git_utils.DetachedHeadPushRemoteHeadChangedError(),
                "publish_remote_head_changed",
                "publish failed: remote head changed",
            ),
            (
                git_utils.DetachedHeadPushRemoteHeadUnchangedError(),
                "publish_remote_head_unchanged",
                "publish failed: remote head unchanged",
            ),
            (
                git_utils.DetachedHeadPushRemoteProbeError(),
                "publish_remote_probe_failed",
                "publish failed: remote head probe failed",
            ),
        ],
    )
    def test_push_dispatch_classifies_detached_publish_failures(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        push_error: Exception,
        failure_kind: str,
        error: str,
    ) -> None:
        """Lease publication failures become durable results instead of worker crashes."""
        job = GitJob(
            repo="test/repo",
            op="push",
            timeout_s=60,
            kwargs={"cwd": Path("/tmp/wt"), "branch": "7-auto"},
        )
        with patch(
            "hephaestus.automation.git_utils.push_current_branch_with_lease_on_divergence",
            side_effect=push_error,
        ):
            pool.submit(job, StageName.MERGE_WAIT)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == error
        assert result.value == {"failure_kind": failure_kind}

    def test_release_branch_reservation_dispatches_conditional_delete(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Terminal cleanup reports a stale reservation without force-deleting it."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="release_branch_reservation",
            timeout_s=60,
            kwargs={
                "branch": "7-auto",
                "base_sha": pin,
                "repo_root": str(tmp_path),
            },
        )
        with patch(
            "hephaestus.automation.pipeline.git_cleanup.delete_reserved_branch_if_unchanged",
            return_value=False,
        ) as release:
            pool.submit(job, StageName.FINISHED)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value is False
        release.assert_called_once_with(
            "7-auto",
            pin,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
            revalidate_remote=ANY,
        )

    def test_release_branch_reservation_accepts_sha256_object_id(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Cleanup supports repositories that use SHA-256 object IDs."""
        pin = "a" * 64
        job = GitJob(
            repo="test/repo",
            op="release_branch_reservation",
            timeout_s=60,
            kwargs={"branch": "7-auto", "base_sha": pin, "repo_root": str(tmp_path)},
        )
        with patch(
            "hephaestus.automation.pipeline.git_cleanup.delete_reserved_branch_if_unchanged",
            return_value=True,
        ) as release:
            pool.submit(job, StageName.FINISHED)
            _, result = completion_q.get(timeout=10)

        assert result.ok
        release.assert_called_once_with(
            "7-auto",
            pin,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
            revalidate_remote=ANY,
        )

    def test_commit_push_extracts_explicit_keys(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """commit_push passes only accepted keys ('branch' must not crash it)."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 5,
                "worktree_path": tmp_path,
                "branch": "5-auto",
                "agent": "claude",
                "agent_model": "sol:medium",
                "pi_dir": Path("/private/pi-agent"),
            },
        )
        remote_env = {"GIT_CONFIG_GLOBAL": os.devnull}
        remote_config = ("-c", "credential.helper=!trusted-gh auth git-credential")
        with (
            patch(
                "hephaestus.automation.git_utils.commit_if_changes", return_value=True
            ) as mock_commit,
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ) as authentication,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_commit.assert_called_once_with(
            5,
            tmp_path,
            "claude",
            allowed_paths=None,
            timeout=60,
            agent_model="sol:medium",
            pi_dir=Path("/private/pi-agent"),
            git_message_timeout=1200,
            signing_env_factory=mock_commit.call_args.kwargs["signing_env_factory"],
        )
        authentication.assert_called_once_with(
            cwd=tmp_path,
            expected_repo="test/repo",
            timeout=60,
        )
        mock_push.assert_called_once_with(
            "5-auto",
            tmp_path,
            timeout=60,
            env=remote_env,
            remote_config=remote_config,
        )
        assert result.ok is True
        assert result.value == {"pushed": True, "head_sha": "b" * 40}

    def test_dirty_commit_push_passes_controlled_signing_env_to_commit_helper(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A dirty worker passes the exact host signing environment to the commit."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=73,
            kwargs={
                "issue_number": 2874,
                "worktree_path": tmp_path,
                "branch": "2874-sign-commits",
                "agent": "claude",
                "agent_model": "sol:medium",
                "git_message_timeout": 321,
            },
        )
        signing_env = {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "commit.gpgsign",
            "GIT_CONFIG_VALUE_0": "true",
        }
        remote_env = {"GIT_CONFIG_GLOBAL": os.devnull}
        remote_config = ("-c", "credential.helper=!trusted-gh auth git-credential")
        with (
            patch(
                "hephaestus.automation.git_utils.run",
                return_value=MagicMock(stdout=" M pending.py\n"),
            ),
            patch(
                f"{_WP}._controlled_git_signing_env", return_value=signing_env
            ) as controlled_signing,
            patch("hephaestus.automation.pr_manager.commit_changes") as commit,
            patch("hephaestus.automation.git_utils.push_branch") as push,
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=(remote_env, remote_config),
            ),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        controlled_signing.assert_called_once_with(tmp_path, timeout=73)
        commit.assert_called_once_with(
            2874,
            tmp_path,
            "claude",
            allowed_paths=None,
            agent_model="sol:medium",
            git_timeout=73,
            git_message_timeout=321,
            signing_env=signing_env,
        )
        push.assert_called_once_with(
            "2874-sign-commits",
            tmp_path,
            timeout=73,
            env=remote_env,
            remote_config=remote_config,
        )
        assert result.ok is True
        assert result.value == {"pushed": True, "head_sha": "b" * 40}

    def test_commit_push_rejects_precommitted_unplanned_2472_edit_before_push(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A host allowlist blocks #2472 even when the agent committed it first."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 2472,
                "worktree_path": tmp_path,
                "branch": "2472-auto-impl",
                "agent": "codex",
                "allowed_paths": ("hephaestus/automation/claude_invoke.py",),
                "scope_history_base_sha": "a" * 40,
            },
        )
        with (
            patch(
                "hephaestus.automation.git_utils.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout="scripts/run_ci_local.sh\0"),
                ],
            ),
            patch("hephaestus.automation.pr_manager.commit_changes") as commit,
            patch("hephaestus.automation.git_utils.push_branch") as push,
            patch.object(pool, "_verify_publish_git_metadata", return_value=None),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "implementation changed paths outside approved scope"
        commit.assert_not_called()
        push.assert_not_called()

    def test_direct_scope_commit_push_uses_its_base_pin_without_an_upstream(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A newly reserved direct writer validates committed paths from its base pin."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 2472,
                "worktree_path": tmp_path,
                "branch": "2472-auto-impl",
                "agent": "codex",
                "allowed_paths": ("hephaestus/automation/claude_invoke.py",),
                "expected_repo": "test/repo",
                "git_metadata_receipt": "b" * 64,
                "expected_remote_sha": pin,
                "scope_history_base_sha": pin,
            },
        )
        with (
            patch(
                "hephaestus.automation.git_utils.run",
                side_effect=[
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess(
                        [], 0, stdout="hephaestus/automation/claude_invoke.py\0"
                    ),
                    subprocess.CompletedProcess([], 0, stdout=f"{tmp_path}\n"),
                    subprocess.CompletedProcess([], 1, stdout=""),
                    subprocess.CompletedProcess([], 1, stdout=""),
                    subprocess.CompletedProcess([], 1, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout="https://github.com/test/repo.git\n"),
                ],
            ),
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=True),
            patch("hephaestus.automation.git_utils.push_branch_if_remote_matches") as push,
            patch.object(pool, "_validate_publish_boundary", return_value=None),
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
            patch.object(
                WorkerPool,
                "_trusted_git_metadata_receipt",
                return_value={"git_metadata_receipt": "b" * 64},
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        push.assert_called_once_with(
            "2472-auto-impl",
            pin,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
        )

    def test_commit_push_fails_before_commit_when_signing_is_unavailable(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A dirty worktree cannot stage or commit without a validated signing identity."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5, "worktree_path": tmp_path, "branch": "5-auto"},
        )
        signing_failure = JobResult(
            ok=False,
            value={"failure_kind": "signing_configuration"},
            error="host signing configuration unavailable",
        )
        with (
            patch(
                "hephaestus.automation.git_utils.run",
                return_value=MagicMock(stdout=" M pending.py\\n"),
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool._controlled_git_signing_env",
                return_value=signing_failure,
            ),
            patch("hephaestus.automation.pr_manager.commit_changes") as commit,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        commit.assert_not_called()
        assert result.ok is False
        assert result.value == {"failure_kind": "signing_configuration"}
        assert result.error == "host signing configuration unavailable"

    def test_publish_rejects_git_metadata_changed_after_agent_run(self, tmp_path: Path) -> None:
        """A Codex job cannot alter common Git controls before host publication."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "allowed_paths": ("hephaestus/automation/guard.py",),
                "expected_repo": "test/repo",
                "git_metadata_receipt": "a" * 64,
            },
        )

        with patch.object(
            WorkerPool,
            "_trusted_git_metadata_receipt",
            return_value={"git_metadata_receipt": "b" * 64},
        ):
            result = WorkerPool._verify_publish_git_metadata(job, tmp_path)

        assert result is not None
        assert result.error == "publication Git metadata changed after agent run"

    def test_commit_push_rejects_incomplete_scope_retraction_before_publish(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """An address agent cannot publish an unrelated feature it merely repaired."""
        base_sha = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 2137,
                "worktree_path": tmp_path,
                "branch": "2137-auto-impl",
                "agent": "claude",
                "scope_retraction_base_sha": base_sha,
                "scope_retraction_paths": ("hephaestus/agents/runtime.py",),
            },
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=True),
            patch(
                "hephaestus.automation.git_utils.run",
                return_value=subprocess.CompletedProcess(
                    [], 0, stdout="hephaestus/agents/runtime.py\n"
                ),
            ) as diff,
            patch("hephaestus.automation.git_utils.push_branch") as push,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {"scope_retraction_failure": True}
        assert result.error == "scope retraction incomplete"
        diff.assert_called_once_with(
            [
                "git",
                "--literal-pathspecs",
                "diff",
                "--name-only",
                base_sha,
                "HEAD",
                "--",
                "hephaestus/agents/runtime.py",
            ],
            cwd=tmp_path,
            capture_output=True,
            timeout=60,
        )
        push.assert_not_called()

    def test_commit_push_rejects_pathspec_magic_before_publish(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Git pathspec magic cannot turn a required retraction into an empty diff."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 2137,
                "worktree_path": tmp_path,
                "branch": "2137-auto-impl",
                "agent": "claude",
                "scope_retraction_base_sha": "a" * 40,
                "scope_retraction_paths": (":(exclude,glob)**",),
            },
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=True),
            patch("hephaestus.automation.git_utils.run") as diff,
            patch("hephaestus.automation.git_utils.push_branch") as push,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.value == {"scope_retraction_failure": True}
        assert result.error == "scope retraction verification unavailable"
        diff.assert_not_called()
        push.assert_not_called()

    def test_direct_scope_commit_push_requires_unchanged_remote_reservation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Direct scopes publish only through the server-side reservation lease."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 5,
                "worktree_path": tmp_path,
                "branch": "5-auto",
                "agent": "claude",
                "expected_remote_sha": pin,
            },
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=True),
            patch("hephaestus.automation.git_utils.push_branch_if_remote_matches") as strict_push,
            patch("hephaestus.automation.git_utils.push_branch") as normal_push,
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"pushed": True, "head_sha": "b" * 40}
        strict_push.assert_called_once_with(
            "5-auto", pin, tmp_path, timeout=60, env=ANY, remote_config=ANY
        )
        normal_push.assert_not_called()

    def test_direct_scope_no_commit_releases_unchanged_reservation(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """An unused reservation is conditionally deleted instead of blocking reruns."""
        pin = "a" * 40
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 5,
                "worktree_path": tmp_path,
                "branch": "5-auto",
                "agent": "claude",
                "expected_remote_sha": pin,
            },
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=False),
            patch(
                "hephaestus.automation.git_utils.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="0\n"),
            ),
            patch("hephaestus.automation.git_utils.delete_reserved_branch_if_unchanged") as release,
            patch("hephaestus.automation.git_utils.push_branch") as normal_push,
            patch.object(pool, "_read_publish_head", return_value=pin) as read_head,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == {"pushed": False, "head_sha": pin}
        release.assert_called_once_with(
            "5-auto",
            pin,
            tmp_path,
            timeout=60,
            env=ANY,
            remote_config=ANY,
            revalidate_remote=ANY,
        )
        read_head.assert_called_once_with(tmp_path, timeout=60)
        normal_push.assert_not_called()

    def test_commit_push_returns_clean_head_without_pushing_when_nothing_committed(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """commit_push reports the verified clean head without pushing it."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5, "worktree_path": tmp_path},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=False),
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
            patch.object(pool, "_read_publish_head", return_value="a" * 40),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_push.assert_not_called()
        assert result.ok is True
        assert result.value == {"pushed": False, "head_sha": "a" * 40}

    def test_commit_push_publishes_agent_precommitted_change(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A clean tree ahead of its remote branch still needs coordinator-owned push."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5, "worktree_path": tmp_path, "branch": "5-auto"},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=False),
            patch(
                "hephaestus.automation.git_utils.has_unpushed_commits", return_value=True
            ) as mock_ahead,
            patch("hephaestus.automation.git_utils.run", return_value=MagicMock(stdout="")),
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_ahead.assert_called_once_with("5-auto", tmp_path, timeout=60)
        mock_push.assert_called_once_with(
            "5-auto", tmp_path, timeout=60, env=ANY, remote_config=ANY
        )
        assert result.ok is True
        assert result.value == {"pushed": True, "head_sha": "b" * 40}

    def test_commit_push_does_not_publish_dirty_worktree_after_failed_commit(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A failed commit cannot publish an older unpushed branch tip."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5, "worktree_path": tmp_path, "branch": "5-auto"},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes", return_value=False),
            patch("hephaestus.automation.git_utils.has_unpushed_commits", return_value=True),
            patch(
                "hephaestus.automation.git_utils.run",
                return_value=MagicMock(stdout=" M uncommitted-change.py\\n"),
            ),
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_push.assert_not_called()
        assert result.ok is False
        assert result.error == "commit_push left uncommitted changes"

    def test_commit_push_rejects_retired_detached_reviewer_publication(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A reviewer checkout can never be repurposed as a branch writer."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={
                "issue_number": 5,
                "worktree_path": tmp_path,
                "branch": "5-auto",
                "publish_detached_head": True,
                "expected_remote_sha": "a" * 40,
            },
        )
        with patch("hephaestus.automation.git_utils.commit_if_changes") as commit:
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        commit.assert_not_called()
        assert result.ok is False
        assert result.error == "detached reviewer commit publication is unsupported"

    def test_commit_push_missing_worktree_path_is_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Missing worktree_path is an explicit error, not a silent skip."""
        job = GitJob(
            repo="test/repo",
            op="commit_push",
            timeout_s=60,
            kwargs={"issue_number": 5},
        )
        with (
            patch("hephaestus.automation.git_utils.commit_if_changes") as mock_commit,
            patch("hephaestus.automation.git_utils.push_branch") as mock_push,
        ):
            pool.submit(job, StageName.PR_REVIEW)
            _, result = completion_q.get(timeout=10)

        mock_commit.assert_not_called()
        mock_push.assert_not_called()
        assert result.ok is False
        assert "worktree_path" in result.error

    def test_clone_dispatch_threads_timeout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Clone runs gh repo clone with the job's timeout budget."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once_with(
            ["gh", "repo", "clone", "owner/name", "/tmp/dest"],
            cwd=None,
            timeout=120,
        )
        assert result.ok is True

    def test_sync_checkout_fast_forwards_clean_expected_default_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A reusable checkout is verified, fetched, then fast-forwarded before use."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="0\t1\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n" + "a" * 40 + "\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        ssh_command = _executable_path("ssh", path=os.defpath)
        ssh_config = (
            f"{shlex.quote(ssh_command)} -F {shlex.quote(os.devnull)} "
            "-o BatchMode=yes -o StrictHostKeyChecking=yes"
        )
        assert mock_run.call_args_list == [
            call(
                ["git", "config", "--null", "--list"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "rev-parse", "--git-path", "info/grafts"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "remote", "get-url", "origin"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=checkout,
                check=False,
                log_errors=False,
                timeout=120,
                env=ANY,
            ),
            call(
                [_trusted_gh_executable(), "api", "repos/owner/name", "--jq", ".default_branch"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    f"core.sshCommand={ssh_config}",
                    "-c",
                    "credential.helper=",
                    "-c",
                    (
                        "credential.helper=!"
                        f"{shlex.quote(_trusted_gh_executable() or '')} "
                        "auth git-credential"
                    ),
                    "-c",
                    "core.askPass=",
                    "-c",
                    "http.sslVerify=true",
                    "fetch",
                    "--no-tags",
                    "--no-recurse-submodules",
                    "origin",
                    "+refs/heads/main:refs/remotes/origin/main",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=checkout,
                check=False,
                log_errors=False,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "-c",
                    "core.fsmonitor=false",
                    "merge",
                    "--ff-only",
                    "origin/main",
                ],
                cwd=checkout,
                check=False,
                log_errors=False,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=checkout,
                check=False,
                log_errors=False,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "rev-parse", "HEAD", "origin/main"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
        ]
        assert (checkout / ".git" / ".hephaestus-git-metadata.lock").is_file()
        assert result.ok is True
        assert result.value == "a" * 40

    def test_sync_checkout_missing_gh_explains_extra_root_flag(
        self, pool: WorkerPool, tmp_path: Path
    ) -> None:
        """A missing trusted executable gives the operator the supported escape hatch."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        with (
            patch("hephaestus.automation.git_utils.run") as mock_run,
            patch(f"{_WP}._trusted_gh_executable", return_value=None),
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
            ]
            result = pool._sync_checkout_locked(
                checkout=checkout,
                expected_repo="owner/name",
                timeout_s=120,
            )

        assert result.error == (
            "required GitHub executable is unavailable; pass "
            "--gh-extra-path-root ROOT when ROOT/bin/gh is the intended installation"
        )

    def test_sync_checkout_rechecks_clean_state_after_fetch_before_merge(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A checkout dirtied during sync is rejected before fast-forwarding it."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=" M concurrent-change.py\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "checkout has uncommitted changes" in (result.error or "")
        argvs = [call.args[0] for call in mock_run.call_args_list]
        assert ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"] not in argvs
        assert not any(
            argv[:3] == ["git", "-c", f"core.hooksPath={os.devnull}"] and "merge" in argv
            for argv in argvs
        )

    def test_checkout_state_allows_untracked_files(self, tmp_path: Path) -> None:
        """Untracked files do not block reusable-main synchronization."""
        checkout = tmp_path / "checkout"
        subprocess.run(
            ["git", "init", "-b", "main", str(checkout)],
            check=True,
            capture_output=True,
        )
        (checkout / "intermediate.txt").write_text("pipeline output\n")

        assert (
            WorkerPool._checkout_state_error(
                checkout=checkout,
                default_branch="main",
                timeout_s=120,
            )
            is None
        )

    def test_checkout_state_allows_ignored_intermediate_files(self, tmp_path: Path) -> None:
        """Ignored build and log output do not make a reusable checkout dirty."""
        checkout = tmp_path / "checkout"
        subprocess.run(
            ["git", "init", "-b", "main", str(checkout)],
            check=True,
            capture_output=True,
        )
        (checkout / ".gitignore").write_text("build/\n/*.log\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=checkout, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "test: add ignore rules",
            ],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        (checkout / "output.log").write_text("pipeline output\n")
        (checkout / "build").mkdir()
        (checkout / "build" / "artifact.txt").write_text("generated\n")

        assert (
            WorkerPool._checkout_state_error(
                checkout=checkout,
                default_branch="main",
                timeout_s=120,
            )
            is None
        )

    @pytest.mark.parametrize("staged", [False, True])
    def test_checkout_state_rejects_tracked_changes(self, tmp_path: Path, *, staged: bool) -> None:
        """Tracked staged and unstaged edits block reusable-main synchronization."""
        checkout = tmp_path / "checkout"
        subprocess.run(
            ["git", "init", "-b", "main", str(checkout)],
            check=True,
            capture_output=True,
        )
        tracked = checkout / "tracked.txt"
        tracked.write_text("before\n")
        subprocess.run(["git", "add", "tracked.txt"], cwd=checkout, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test User",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-m",
                "test: add tracked file",
            ],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        tracked.write_text("after\n")
        if staged:
            subprocess.run(["git", "add", "tracked.txt"], cwd=checkout, check=True)

        error = WorkerPool._checkout_state_error(
            checkout=checkout,
            default_branch="main",
            timeout_s=120,
        )

        assert error is not None
        assert "checkout has uncommitted changes" in error
        assert "tracked.txt" in error

    def test_sync_checkout_rejects_dirty_worktree_before_fetching(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A reused checkout with local changes is never modified by the loop."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=" M changed.py\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert mock_run.call_args_list == [
            call(
                ["git", "remote", "get-url", "origin"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
        ]
        assert result.ok is False
        assert "uncommitted changes" in (result.error or "")

    def test_sync_checkout_rejects_unexpected_origin(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A directory for another repository cannot be mistaken for the target checkout."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="https://github.com/other/project.git\n"
            )
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once_with(
            ["git", "remote", "get-url", "origin"], cwd=checkout, timeout=120, env=ANY
        )
        assert result.ok is False
        assert "expected origin owner/name" in (result.error or "")

    def test_sync_checkout_does_not_disclose_an_unexpected_origin(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Credentials embedded in a rejected remote never reach pipeline evidence."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        origin = "https://token:secret@github.com/other/project.git"
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess([], 0, stdout=f"{origin}\n")
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "expected origin owner/name" in (result.error or "")
        assert "token" not in (result.error or "")
        assert "secret" not in (result.error or "")
        assert origin not in (result.error or "")

    def test_sync_checkout_rejects_missing_checkout(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A vanished checkout path fails without running any Git command."""
        checkout = tmp_path / "missing"
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_not_called()
        assert result.ok is False
        assert "does not exist" in (result.error or "")

    def test_sync_checkout_rejects_plaintext_git_origin(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """The unauthenticated ``git://`` transport is never used for synchronization."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="git://github.com/owner/name.git\n"
            )
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once_with(
            ["git", "remote", "get-url", "origin"], cwd=checkout, timeout=120, env=ANY
        )
        assert result.ok is False
        assert "expected origin owner/name" in (result.error or "")

    def test_sync_checkout_synchronizes_ssh_origin_with_controlled_transport(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A valid SSH origin is fetched only with controlled Git configuration."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        monkeypatch.setenv("GIT_ALTERNATE_OBJECT_DIRECTORIES", "/unsafe/objects")
        monkeypatch.setenv("GIT_COMMON_DIR", "/unsafe/common-dir")
        monkeypatch.setenv("GIT_DIR", "/unsafe/git-dir")
        monkeypatch.setenv("GIT_EXEC_PATH", "/unsafe/git-exec-path")
        monkeypatch.setenv("GIT_INDEX_FILE", "/unsafe/index")
        monkeypatch.setenv("GIT_OBJECT_DIRECTORY", "/unsafe/object-dir")
        monkeypatch.setenv("GIT_SSH", "/unsafe/ssh")
        monkeypatch.setenv("GIT_SSH_COMMAND", "/unsafe/ssh-wrapper")
        monkeypatch.setenv("GIT_ASKPASS", "/unsafe/askpass")
        monkeypatch.setenv("SSH_ASKPASS", "/unsafe/ssh-askpass")
        monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
        monkeypatch.setenv("GIT_SSL_CAINFO", "/unsafe/ca.pem")
        monkeypatch.setenv("GIT_SSL_CAPATH", "/unsafe/ca-dir")
        monkeypatch.setenv("GIT_WORK_TREE", "/unsafe/worktree")
        monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
        monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
        monkeypatch.setenv("GIT_CONFIG_VALUE_0", "!/unsafe/credential-helper")
        monkeypatch.setenv("GIT_CONFIG", "/unsafe/git-config")
        monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "credential.helper=!/unsafe/helper")
        monkeypatch.setenv("GIT_NO_REPLACE_OBJECTS", "0")
        monkeypatch.setenv("PATH", "/unsafe/path")
        for key in (
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            monkeypatch.setenv(key, "http://unsafe-proxy")
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="git@github.com:owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="0\t1\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n" + "a" * 40 + "\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        fetch_call = mock_run.call_args_list[4]
        ssh_command = _executable_path("ssh", path=os.defpath)
        ssh_config = (
            f"{shlex.quote(ssh_command)} -F {shlex.quote(os.devnull)} "
            "-o BatchMode=yes -o StrictHostKeyChecking=yes"
        )
        assert fetch_call.args[0] == [
            "git",
            "-c",
            f"core.hooksPath={os.devnull}",
            "-c",
            f"core.sshCommand={ssh_config}",
            "-c",
            "credential.helper=",
            "-c",
            (
                "credential.helper=!"
                f"{shlex.quote(_trusted_gh_executable() or '')} auth git-credential"
            ),
            "-c",
            "core.askPass=",
            "-c",
            "http.sslVerify=true",
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            "origin",
            "+refs/heads/main:refs/remotes/origin/main",
        ]
        fetch_env = fetch_call.kwargs["env"]
        assert fetch_env["GIT_TERMINAL_PROMPT"] == "0"
        for key in (
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_DIR",
            "GIT_SSH",
            "GIT_SSH_COMMAND",
            "GIT_ASKPASS",
            "GIT_EXEC_PATH",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "SSH_ASKPASS",
            "GIT_SSL_NO_VERIFY",
            "GIT_SSL_CAINFO",
            "GIT_SSL_CAPATH",
            "GIT_WORK_TREE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "GIT_CONFIG",
            "GIT_CONFIG_PARAMETERS",
            "ALL_PROXY",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "all_proxy",
            "http_proxy",
            "https_proxy",
        ):
            assert key not in fetch_env
        trusted_git = _trusted_git_executable()
        expected_path_entries = os.defpath.split(os.pathsep)
        if trusted_git is not None:
            trusted_parent = str(Path(trusted_git).parent)
            expected_path_entries = [
                trusted_parent,
                *(entry for entry in expected_path_entries if entry != trusted_parent),
            ]
        assert fetch_env["PATH"] == os.pathsep.join(expected_path_entries)
        assert fetch_env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert fetch_env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert fetch_env["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert mock_run.call_args_list[3] == call(
            [_trusted_gh_executable(), "api", "repos/owner/name", "--jq", ".default_branch"],
            cwd=checkout,
            timeout=120,
            env=ANY,
        )
        git_call_indices = (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11)
        for git_call in (mock_run.call_args_list[index] for index in git_call_indices):
            assert git_call.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
            assert "GIT_DIR" not in git_call.kwargs["env"]
            assert git_call.kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        assert result.ok is True

    def test_sync_checkout_rejects_executable_local_git_config(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Executable checkout-local Git config is rejected before any fetch or merge."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        (checkout / ".git").mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess(
                    [], 0, stdout="filter.payload.process\n/unsafe/filter\0"
                ),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "unsafe local Git configuration" in (result.error or "")
        assert all("fetch" not in call.args[0] for call in mock_run.call_args_list)

    @pytest.mark.parametrize(
        "entry",
        (
            "core.sshCommand\n/unsafe/ssh\0",
            "credential.helper\n!/unsafe/credential-helper\0",
            "include.path\n/unsafe/include\0",
            "includeIf.gitdir:/unsafe/.path\n/unsafe/include\0",
            "merge.payload.driver\n/unsafe/merge\0",
            "http.sslVerify\nfalse\0",
            "http.https://github.com/.sslVerify\nfalse\0",
            "http.sslCAInfo\n/unsafe/ca.pem\0",
            "http.proxy\nhttp://unsafe-proxy\0",
            "url.file:///unsafe/.insteadOf\nhttps://github.com/owner/name\0",
            "credential.https://github.com.helper\n!/unsafe/helper\0",
            "remote.origin.uploadpack\n/unsafe/upload-pack\0",
            "remote.origin.pushurl\nhttps://github.com/attacker/target\0",
            "remote.origin.receivepack\n/unsafe/receive-pack\0",
            "remote.origin.proxy\nhttp://unsafe-proxy\0",
            "remote.origin.proxyAuthMethod\nanyauth\0",
            "fetch.recurseSubmodules\ntrue\0",
            "submodule.recurse\ntrue\0",
            "core.worktree\n/unsafe/worktree\0",
        ),
        ids=(
            "ssh-command",
            "credential-helper",
            "include",
            "conditional-include",
            "merge-driver",
            "disabled-tls",
            "url-scoped-tls",
            "custom-ca",
            "http-proxy",
            "url-rewrite",
            "url-scoped-credential-helper",
            "remote-upload-pack",
            "remote-push-url",
            "remote-receive-pack",
            "remote-proxy",
            "remote-proxy-auth",
            "fetch-recurses-submodules",
            "submodule-recurses",
            "core-worktree",
        ),
    )
    def test_checkout_config_parser_rejects_unsafe_settings(self, entry: str) -> None:
        """Executable, routing, and TLS-affecting checkout config cannot survive scanning."""
        assert _unsafe_local_git_config_key(entry) is not None

    def test_sync_checkout_rejects_unsafe_linked_worktree_config(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Effective config scanning includes a linked worktree's config.worktree."""
        checkout = tmp_path / "checkout"
        linked = tmp_path / "linked"
        subprocess.run(
            ["git", "init", "--initial-branch", "main", str(checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
            subprocess.run(
                ["git", "config", key, value],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "initial"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "-b", "linked", str(linked)],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "extensions.worktreeConfig", "true"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "--worktree", "filter.payload.process", "/unsafe/filter"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
        )
        benign_config = tmp_path / "benign-git-config"
        benign_config.touch()
        monkeypatch.setenv("GIT_CONFIG", str(benign_config))
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(linked)},
        )
        actual_run = git_utils.run

        def run_config_only(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert command == ["git", "config", "--null", "--list"]
            return actual_run(command, **kwargs)

        with patch("hephaestus.automation.git_utils.run", side_effect=run_config_only) as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once()
        assert result.ok is False
        assert "unsafe local Git configuration" in (result.error or "")

    @pytest.mark.usefixtures("require_git_path_format")
    def test_sync_checkout_rejects_grafts_in_linked_worktree(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Legacy grafts in linked-worktree metadata stop sync before ancestry checks."""
        checkout = tmp_path / "checkout"
        linked = tmp_path / "linked"
        subprocess.run(
            ["git", "init", "--initial-branch", "main", str(checkout)],
            check=True,
            capture_output=True,
            text=True,
        )
        for key, value in (("user.name", "Test User"), ("user.email", "test@example.com")):
            subprocess.run(
                ["git", "config", key, value],
                cwd=checkout,
                check=True,
                capture_output=True,
                text=True,
            )
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "initial"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "worktree", "add", "-b", "linked", str(linked)],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        )
        graft_path = Path(
            subprocess.run(
                ["git", "rev-parse", "--git-path", "info/grafts"],
                cwd=linked,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
        if not graft_path.is_absolute():
            graft_path = linked / graft_path
        graft_path.parent.mkdir(parents=True, exist_ok=True)
        graft_path.write_text("# unsafe graft\n", encoding="utf-8")
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(linked)},
        )
        actual_run = git_utils.run

        def run_preflight(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            assert command in (
                ["git", "config", "--null", "--list"],
                ["git", "rev-parse", "--git-path", "info/grafts"],
            )
            return actual_run(command, **kwargs)

        with patch("hephaestus.automation.git_utils.run", side_effect=run_preflight) as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert mock_run.call_count == 2
        assert result.ok is False
        assert "unsafe legacy Git grafts" in (result.error or "")

    def test_sync_checkout_rejects_spoofed_github_hostname(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A hostname merely containing ``github.com`` cannot pass origin validation."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                [], 0, stdout="https://evilgithub.com/owner/name.git\n"
            )
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_called_once_with(
            ["git", "remote", "get-url", "origin"], cwd=checkout, timeout=120, env=ANY
        )
        assert result.ok is False
        assert "expected origin owner/name" in (result.error or "")

    def test_sync_checkout_accepts_clean_attached_checkout_on_nondefault_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A clean attached runner branch may safely follow the default branch."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CompletedProcess([], 0, stdout="0\t0\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="feature\n"),
                subprocess.CompletedProcess([], 0, stdout="a" * 40 + "\n" + "a" * 40 + "\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert result.value == "a" * 40

    def test_sync_checkout_rejects_detached_head_before_fetching(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A detached reusable checkout is rejected before remote metadata or mutation."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 1, stdout=""),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert mock_run.call_args_list == [
            call(
                ["git", "remote", "get-url", "origin"],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                [
                    "git",
                    "-c",
                    "core.fsmonitor=false",
                    "status",
                    "--porcelain",
                    "--untracked-files=no",
                ],
                cwd=checkout,
                timeout=120,
                env=ANY,
            ),
            call(
                ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
                cwd=checkout,
                check=False,
                log_errors=False,
                timeout=120,
                env=ANY,
            ),
        ]
        assert result.ok is False
        assert "detached" in (result.error or "")

    def test_sync_checkout_waits_for_shared_git_metadata_lock(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Sync cannot fetch while a shared worktree-metadata operation is active."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        metadata_lock = checkout / ".git" / ".hephaestus-git-metadata.lock"
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=0,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with (
            file_lock(metadata_lock),
            patch("hephaestus.automation.git_utils.run") as mock_run,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "lock_timeout"
        assert mock_run.call_args_list == [
            call(
                ["git", "config", "--null", "--list"],
                cwd=checkout,
                timeout=0,
                env=ANY,
            ),
            call(
                ["git", "rev-parse", "--git-path", "info/grafts"],
                cwd=checkout,
                timeout=0,
                env=ANY,
            ),
        ]

    def test_sync_checkout_rejects_local_commits_ahead_of_remote(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Clean local commits are not mistaken for a synchronized checkout."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="1\t0\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert mock_run.call_args_list[-1] == call(
            ["git", "rev-list", "--left-right", "--count", "HEAD...origin/main"],
            cwd=checkout,
            timeout=120,
            env=ANY,
        )
        assert result.ok is False
        assert "local commits" in (result.error or "")

    def test_sync_checkout_rejects_unknown_remote_default_branch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Missing GitHub default-branch metadata fails before a checkout mutation."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert mock_run.call_args_list[-1] == call(
            [_trusted_gh_executable(), "api", "repos/owner/name", "--jq", ".default_branch"],
            cwd=checkout,
            timeout=120,
            env=ANY,
        )
        assert result.ok is False
        assert "default branch" in (result.error or "")

    def test_sync_checkout_reports_failed_fast_forward(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A failed fast-forward cannot be treated as a ready checkout."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="0\t1\n"),
                subprocess.CompletedProcess([], 1),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "cannot fast-forward" in (result.error or "")

    def test_sync_checkout_rejects_post_merge_head_mismatch(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A successful merge still needs to leave HEAD at the fetched remote tip."""
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        job = GitJob(
            repo="test/repo",
            op="sync_checkout",
            timeout_s=120,
            kwargs={"repo": "owner/name", "dest": str(checkout)},
        )
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="0\t1\n"),
                subprocess.CompletedProcess([], 0),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="old-head\nnew-head\n"),
            ]
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert "did not reach origin/main" in (result.error or "")

    @pytest.mark.parametrize(
        "kwargs",
        [{}, {"repo": "owner/name"}, {"dest": "/tmp/dest"}, {"repo": "", "dest": ""}],
    )
    def test_clone_missing_args_fast_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        kwargs: dict[str, str],
    ) -> None:
        """Clone with empty repo/dest fails fast without shelling out."""
        job = GitJob(repo="test/repo", op="clone", timeout_s=60, kwargs=kwargs)
        with patch("hephaestus.automation.git_utils.run") as mock_run:
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        mock_run.assert_not_called()
        assert result.ok is False
        assert "clone requires" in result.error

    def test_git_timeout_returns_error(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A git helper hitting its timeout maps to error='timeout'."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=1,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        with patch(
            "hephaestus.automation.git_utils.run",
            side_effect=subprocess.TimeoutExpired(cmd=["gh"], timeout=1),
        ):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "timeout"

    def test_git_called_process_error_returns_rc_and_tails(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """Git CalledProcessError maps to rc=<n> with stdout/stderr tails."""
        job = GitJob(
            repo="test/repo",
            op="clone",
            timeout_s=60,
            kwargs={"repo": "owner/name", "dest": "/tmp/dest"},
        )
        exc = subprocess.CalledProcessError(
            returncode=128,
            cmd=["gh", "repo", "clone", "owner/name", "/tmp/dest"],
            output="clone stdout tail",
            stderr="fatal: repository access denied",
        )

        with patch("hephaestus.automation.git_utils.run", side_effect=exc):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is False
        assert result.error == "rc=128"
        assert result.stdout_tail == "clone stdout tail"
        assert result.stderr_tail == "fatal: repository access denied"

    def test_unknown_op_fallback(self, pool: WorkerPool) -> None:
        """The defensive unknown-op branch returns an error result.

        Unreachable via GitJob.__post_init__ validation, so exercised by
        bypassing the constructor.
        """
        bogus = MagicMock(spec=GitJob)
        bogus.op = "bogus"
        bogus.repo = "test/repo"
        bogus.kwargs = {}
        result = pool._dispatch_git_op(cast(GitJob, bogus))
        assert result.ok is False
        assert "unknown op" in (result.error or "")


class TestGitLocking:
    """Tests for per-repo serialization and cross-process file locking."""

    def test_same_repo_jobs_serialize_with_mutex(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Two GitJobs for the same repo run serially (held by lock)."""
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )

        events: list[str] = []
        lock = threading.Lock()
        barrier = threading.Barrier(2)  # Both jobs reach here simultaneously

        def git_job_entrypoint(job_name: str) -> None:
            # NOTE: _run_git already holds the per-repo lock around this
            # call — re-acquiring pool._repo_lock here would self-deadlock
            # (threading.Lock is not reentrant). The barrier alone proves
            # serialization: if the pool serialized us, the two jobs never
            # overlap, so neither can satisfy the 2-party barrier.
            with lock:
                events.append(f"{job_name}:entered_lock")
            try:
                barrier.wait(timeout=2.0)
            except threading.BrokenBarrierError:
                # Expected under serialization: the peer never arrives
                # while we are inside the pool's critical section.
                with lock:
                    events.append(f"{job_name}:barrier_failed_expected")
            with lock:
                events.append(f"{job_name}:exited_lock")

        job1 = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})
        job2 = GitJob(repo="test/repo", op="remove_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.side_effect = lambda **kwargs: git_job_entrypoint("job1")
        instance.remove_worktree.side_effect = lambda **kwargs: git_job_entrypoint("job2")

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job1, StageName.REPO)
            pool.submit(job2, StageName.REPO)
            # Block on the completion channel instead of sleeping: robust
            # under the slow pure-Python coverage tracer and proves both
            # jobs actually complete.
            completions = [completion_q.get(timeout=10.0) for _ in range(2)]

        pool.shutdown()

        assert len(completions) == 2

        # Verify serialization: both jobs must have failed the barrier —
        # they never overlapped inside the pool's critical section.
        assert len([e for e in events if e.endswith(":barrier_failed_expected")]) == 2, events

    def test_different_repo_jobs_run_concurrently(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Two GitJobs for different repos overlap (different locks)."""
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=2,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        barrier = threading.Barrier(2)

        def wait_at_barrier(**kwargs: object) -> None:
            # Both jobs must be inside their critical sections at once to
            # satisfy the barrier; a 10 s timeout fails the test if the pool
            # wrongly serialized different repos.
            barrier.wait(timeout=10)

        job1 = GitJob(repo="test/repo1", op="create_worktree", timeout_s=60, kwargs={})
        job2 = GitJob(repo="test/repo2", op="create_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.side_effect = wait_at_barrier

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job1, StageName.REPO)
            pool.submit(job2, StageName.REPO)
            completions = [completion_q.get(timeout=10.0) for _ in range(2)]

        pool.shutdown()

        assert all(result.ok for _, result in completions)

    def test_different_repo_jobs_use_different_locks(
        self,
        pool: WorkerPool,
    ) -> None:
        """Two active GitJob repo contexts use different in-process locks."""
        with pool._repo_lock("test/repo1"), pool._repo_lock("test/repo2"):
            with pool._repo_locks_guard:
                lock1 = pool._repo_locks["test/repo1"].lock
                lock2 = pool._repo_locks["test/repo2"].lock

        assert lock1 is not lock2
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_repo_lock_evicted_after_git_job_completes(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A completed GitJob does not leave an idle repo lock cached forever."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})

        instance = MagicMock()
        instance.create_worktree.return_value = None
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_repo_lock_not_evicted_while_waiter_holds_it(
        self,
        pool: WorkerPool,
    ) -> None:
        """A waiting same-repo user keeps the shared lock entry until it exits."""
        waiter_acquired = threading.Event()
        release_waiter = threading.Event()

        def wait_for_users(expected: int) -> None:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with pool._repo_locks_guard:
                    entry = pool._repo_locks.get("test/repo")
                    if entry is not None and entry.users == expected:
                        return
                time.sleep(0.01)
            pytest.fail(f"repo lock users never reached {expected}")

        def waiter() -> None:
            with pool._repo_lock("test/repo"):
                waiter_acquired.set()
                release_waiter.wait(timeout=5.0)

        with pool._repo_lock("test/repo"):
            with pool._repo_locks_guard:
                entry = pool._repo_locks["test/repo"]
            thread = threading.Thread(target=waiter)
            thread.start()
            wait_for_users(2)

        assert waiter_acquired.wait(timeout=5.0)
        with pool._repo_locks_guard:
            assert pool._repo_locks.get("test/repo") is entry

        release_waiter.set()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        with pool._repo_locks_guard:
            assert "test/repo" not in pool._repo_locks

    def test_repo_lock_path_anchors_at_state_dir(self) -> None:
        """Default lock path is anchored at repo_root/DEFAULT_STATE_DIR, not CWD."""
        expected = get_repo_root() / DEFAULT_STATE_DIR / "locks" / "git-a_b.lock"
        assert _repo_lock_path("a/b") == expected

    def test_repo_lock_path_honors_override(self, tmp_path: Path) -> None:
        """An explicit lock_dir overrides the state-dir anchor (test seam)."""
        assert _repo_lock_path("a/b", tmp_path) == tmp_path / "git-a_b.lock"

    def test_git_job_takes_cross_process_file_lock(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Running a GitJob creates the per-repo sentinel file in lock_dir."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})
        instance = MagicMock()
        instance.create_worktree.return_value = None
        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            pool.submit(job, StageName.REPO)
            _, result = completion_q.get(timeout=10)

        assert result.ok is True
        assert (tmp_path / "locks" / "git-test_repo.lock").exists()

    def test_git_file_lock_timeout_returns_lock_timeout_and_releases_repo_lock(
        self,
        pool: WorkerPool,
        tmp_path: Path,
    ) -> None:
        """A held cross-process lock fails fast with lock_timeout."""
        fcntl = pytest.importorskip("fcntl")
        lock_path = _repo_lock_path("test/repo", tmp_path / "locks")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        held_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=0, kwargs={})

        try:
            fcntl.flock(held_fd, fcntl.LOCK_EX)
            with patch(f"{_WP}.WorktreeManager") as manager:
                result = pool._run_git(job)
        finally:
            fcntl.flock(held_fd, fcntl.LOCK_UN)
            os.close(held_fd)

        manager.assert_not_called()
        assert result.ok is False
        assert result.error == "lock_timeout"
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_git_file_lock_wait_is_interrupted_by_shutdown(
        self,
        pool: WorkerPool,
        shutdown_event: threading.Event,
    ) -> None:
        """Shutdown while waiting for the file lock returns an interrupted result."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=60, kwargs={})

        def interrupting_wait(timeout: float | None = None) -> bool:
            shutdown_event.set()
            return True

        with (
            patch(f"{_WP}.file_lock", side_effect=LockUnavailableError("held")),
            patch.object(shutdown_event, "wait", side_effect=interrupting_wait),
            patch(f"{_WP}.WorktreeManager") as manager,
        ):
            result = pool._run_git(job)

        manager.assert_not_called()
        assert result.ok is False
        assert result.interrupted is True
        assert result.error == "interrupted_waiting_for_git_lock"
        with pool._repo_locks_guard:
            assert pool._repo_locks == {}

    def test_git_file_lock_wait_does_not_swallow_dispatch_lock_errors(
        self,
        pool: WorkerPool,
    ) -> None:
        """Only outer lock acquisition failures are mapped to lock_timeout."""
        job = GitJob(repo="test/repo", op="create_worktree", timeout_s=0, kwargs={})
        instance = MagicMock()
        instance.create_worktree.side_effect = LockUnavailableError("inner lock")

        with patch(f"{_WP}.WorktreeManager", return_value=instance):
            with pytest.raises(LockUnavailableError, match="inner lock"):
                pool._run_git(job)


class TestShutdownAndCancel:
    """Tests for shutdown behavior and future cancellation."""

    def test_shutdown_cancels_queued_job_and_emits_no_completion_for_it(
        self,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """Cancelled queued jobs emit NO completion; the running one completes.

        A slow job occupies the single worker while a second job sits queued;
        shutdown(cancel_futures=True) cancels the queued one. Exactly one
        completion (the running job's, marked interrupted) must arrive.
        """
        shutdown_event = threading.Event()
        pool = WorkerPool(
            size=1,
            shutdown=shutdown_event,
            completion_q=completion_q,
            lock_dir=tmp_path / "locks",
        )
        started = threading.Event()
        release = threading.Event()

        def slow_builder() -> str:
            started.set()
            release.wait(timeout=10)
            return "prompt"

        slow_job = _agent_job(prompt_builder=slow_builder)
        queued_job = BuildTestJob(
            repo="test/repo",
            cwd=Path("/tmp"),
            argv=("echo", "never-runs"),
            timeout_s=60,
        )

        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke.invoke_claude_with_session") as mock_invoke,
        ):
            mock_invoke.return_value = ("done", "sid")
            pool.submit(slow_job, StageName.PLANNING)
            assert started.wait(timeout=10), "slow job never started"
            pool.submit(queued_job, StageName.PR_REVIEW)  # queued behind the busy worker
            pool.shutdown()  # sets shutdown event + cancel_futures=True
            assert shutdown_event.is_set()
            release.set()

            handle, result = completion_q.get(timeout=10)

        # Exactly the running job's completion arrives ...
        assert handle.job is slow_job
        assert result.interrupted is True  # shutdown was set mid-flight
        # ... and NONE for the cancelled queued job.
        with pytest.raises(queue.Empty):
            completion_q.get(timeout=0.5)


class TestOnFutureDone:
    """Tests for the completion-loss guarantees of _on_future_done."""

    def test_cancelled_future_emits_no_completion(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
    ) -> None:
        """A cancelled future synthesizes no completion tuple."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.PR_REVIEW,
        )
        future: Future[JobResult] = Future()
        future.cancel()
        pool._on_future_done(handle, future)
        assert completion_q.empty()

    @pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit(3), GeneratorExit()])
    def test_run_converts_process_control_escape_with_worker_id(
        self, pool: WorkerPool, exc: BaseException
    ) -> None:
        """Escapes inside the worker preserve the executing worker identity."""
        job = BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1)

        with patch.object(pool, "_run_build_test", side_effect=exc):
            result = pool._run(job, claim_key="r#1", claim_stage="ci")

        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith(f"worker_crash: {type(exc).__name__}")
        assert result.worker_id == threading.current_thread().name

    def test_exception_future_emits_worker_crash_completion_and_logs_traceback(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """RuntimeError from future.result() becomes worker_crash with traceback."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.PR_REVIEW,
        )
        future: Future[JobResult] = Future()
        future.set_exception(RuntimeError("boom"))

        with caplog.at_level(logging.INFO, logger=_WP):
            pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error.startswith("worker_crash: RuntimeError")
        assert any(
            record.levelno == logging.ERROR and record.exc_info is not None
            for record in caplog.records
        )

    @pytest.mark.parametrize(
        ("exc", "expected_level"),
        [
            (KeyboardInterrupt(), logging.WARNING),
            (SystemExit(3), logging.INFO),
            (GeneratorExit(), logging.INFO),
        ],
    )
    def test_process_control_future_emits_worker_crash_completion_without_traceback(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        caplog: pytest.LogCaptureFixture,
        exc: BaseException,
        expected_level: int,
    ) -> None:
        """Process-control escapes stay at lower severity and do not log tracebacks."""
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.PR_REVIEW,
        )
        future: Future[JobResult] = Future()
        future.set_exception(exc)

        with caplog.at_level(logging.INFO, logger=_WP):
            pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error.startswith(f"worker_crash: {type(exc).__name__}")
        assert any(record.levelno == expected_level for record in caplog.records)
        assert not any(record.exc_info is not None for record in caplog.records)

    def test_raising_future_emits_truncated_worker_crash_completion(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker crash message longer than the cap is truncated once."""
        small_err_max = 40
        monkeypatch.setattr(f"{_WP}._ERR_MAX", small_err_max)
        handle = JobHandle(
            job=BuildTestJob(repo="r", cwd=Path("/tmp"), argv=("true",), timeout_s=1),
            on_done_state=StageName.PR_REVIEW,
        )
        future: Future[JobResult] = Future()
        future.set_exception(RuntimeError("w" * 200))

        pool._on_future_done(handle, future)

        got_handle, result = completion_q.get_nowait()
        assert got_handle is handle
        assert result.ok is False
        assert result.error is not None
        assert result.error.startswith("worker_crash: RuntimeError: ")
        assert len(result.error) == small_err_max


@pytest.mark.skipif(
    not hasattr(os, "killpg"), reason="process-group termination unavailable on this platform"
)
class TestShutdownReapsSubprocess:
    """WorkerPool.shutdown() SIGTERMs in-flight agent process groups (#2059)."""

    def test_shutdown_terminates_registered_pi_adapter_subprocess_fast(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A registered Pi adapter exposes its child to worker-pool cleanup."""
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]

        class Adapter:
            def invoke(
                self,
                *,
                policy: ExecutionPolicy,
                command: list[str],
                environment: dict[str, str],
                prompt: str,
                cwd: Path,
                timeout: int,
                model: str,
                session_id: str | None,
                process_tracker: agent_runtime.ProcessTracker | None,
            ) -> AgentRunResult:
                del policy, environment, prompt, model, session_id
                assert process_tracker is not None
                process = subprocess.Popen(
                    sleeper,
                    cwd=cwd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                )
                with process_tracker(process.pid):
                    stdout, stderr = process.communicate(timeout=timeout)
                if process.returncode:
                    raise subprocess.CalledProcessError(
                        process.returncode,
                        command,
                        output=stdout,
                        stderr=stderr,
                    )
                return AgentRunResult(
                    stdout=stdout,
                    stderr=stderr,
                    session_id="pi-session",
                )

        monkeypatch.setattr(agent_runtime, "_PI_ISOLATION_ADAPTER", None)
        monkeypatch.setattr(
            agent_runtime,
            "_require_pi_automation_admission",
            lambda _cwd, **_kwargs: PiPreflightResult.ready_result(
                InventoryResult(True, "ready", {}, {}),
                executable=Path(sys.executable).resolve(),
            ),
        )
        monkeypatch.setenv("HEPH_PI_PROVIDER", "operator-provider")
        agent_runtime.register_pi_isolation_adapter(Adapter())
        request = ExecutionRequest(
            AgentRole.IMPLEMENTER,
            AgentOperation.IMPLEMENT,
            SessionLifecycle.START_NEW,
        )
        job = _agent_job(
            agent="pi",
            model="reap-test",
            timeout_s=60,
            session_agent="implementer",
            cwd=tmp_path,
            execution_request=request,
        )

        with patch(f"{_WP}.resolve_agent", return_value="pi"):
            pool.submit(job, StageName.IMPLEMENTATION)
            deadline = time.monotonic() + 10
            while subprocess_registry.live_count() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert subprocess_registry.live_count() == 1, "Pi subprocess never registered"

            t0 = time.monotonic()
            pool.shutdown()
            _handle, result = completion_q.get(timeout=10)
            elapsed = time.monotonic() - t0

        assert elapsed < 15, f"shutdown did not reap Pi fast ({elapsed:.1f}s)"
        assert subprocess_registry.live_count() == 0
        assert result.ok is False
        assert result.interrupted is True

    def test_shutdown_terminates_running_codex_subprocess_fast(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A direct Codex session is registered and reaped with the worker pool."""
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
        job = _agent_job(
            agent="codex",
            model="reap-test",
            timeout_s=60,
            session_agent="implementer",
            cwd=tmp_path,
        )
        with (
            patch(f"{_WP}.resolve_agent", return_value="codex"),
            patch("hephaestus.agents.runtime._codex_base_cmd", return_value=sleeper),
            patch(
                "hephaestus.agents.runtime._codex_automation_profile",
                return_value=nullcontext({"CODEX_HOME": str(tmp_path), "PATH": os.defpath}),
            ),
            patch(
                "hephaestus.agents.runtime.macos_isolated_command",
                side_effect=lambda argv, **_kwargs: list(argv),
            ),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            deadline = time.monotonic() + 10
            while subprocess_registry.live_count() == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert subprocess_registry.live_count() == 1, "Codex subprocess never registered"

            t0 = time.monotonic()
            pool.shutdown()
            _handle, result = completion_q.get(timeout=10)
            elapsed = time.monotonic() - t0

        assert elapsed < 15, f"shutdown did not reap Codex fast ({elapsed:.1f}s)"
        assert result.ok is False
        assert result.interrupted is True

    def test_shutdown_terminates_running_agent_subprocess_fast(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        tmp_path: Path,
    ) -> None:
        """A slow claude job is reaped by shutdown() instead of running to timeout.

        Regression for the #2059 leak: before the fix, ``pool.shutdown()`` only
        cancelled un-started futures — a job already blocked in a claude
        subprocess kept running. Now the child is spawned via the real
        ``_run_tracked`` (process group + registry) and SIGTERMed on shutdown.
        """
        from hephaestus.automation import claude_invoke

        started = threading.Event()
        real_run_tracked = claude_invoke._run_tracked
        # A 60s sleeper stands in for a wedged claude reviewer.
        sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]

        def fake_run_tracked(_cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            started.set()
            # Swap the "claude" binary for a real long-lived Python sleeper but
            # keep the REAL _run_tracked spawn (Popen + process-group tracking).
            return real_run_tracked(list(sleeper), **kwargs)

        job = _agent_job(
            model="reap-test",
            timeout_s=60,
            session_agent="implementer",
            cwd=tmp_path,
        )
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(f"{_WP}.claude_invoke._run_tracked", side_effect=fake_run_tracked),
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            assert started.wait(timeout=10), "agent subprocess never started"
            time.sleep(0.2)  # let the child settle inside communicate()

            t0 = time.monotonic()
            pool.shutdown()
            _handle, result = completion_q.get(timeout=10)
            elapsed = time.monotonic() - t0

        # Reaped well under the 60s sleep (SIGTERM, not timeout).
        assert elapsed < 15, f"shutdown did not reap the subprocess fast ({elapsed:.1f}s)"
        assert result.ok is False
        assert result.interrupted is True


class TestAgentToolScopes:
    """Worker pool passes explicit least-privilege scopes to Claude (#2160)."""

    def _invoke_kwargs(
        self,
        pool: WorkerPool,
        completion_q: CompletionQueue,
        session_agent: str,
    ) -> dict[str, Any]:
        """Submit a Claude job for ``session_agent`` and return invoke kwargs."""
        job = _agent_job(session_agent=session_agent)
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("out", "sid"),
            ) as invoke,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)
        assert result.ok is True
        return cast("dict[str, Any]", invoke.call_args.kwargs)

    def test_reviewer_job_gets_read_only_scope(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        kwargs = self._invoke_kwargs(pool, completion_q, AGENT_PR_REVIEWER)
        assert kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert kwargs["permission_mode"] == "dontAsk"

    def test_implementer_job_gets_write_scope(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        kwargs = self._invoke_kwargs(pool, completion_q, AGENT_IMPLEMENTER)
        assert kwargs["allowed_tools"] == "Read,Write,Edit,Glob,Grep,Bash"
        assert kwargs["permission_mode"] == "dontAsk"

    def test_unmapped_agent_fails_closed_to_read_only(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        kwargs = self._invoke_kwargs(pool, completion_q, "mystery-agent")
        assert kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert kwargs["permission_mode"] == "dontAsk"

    def test_read_only_sandbox_clamps_write_agent_to_read_only(
        self, pool: WorkerPool, completion_q: CompletionQueue
    ) -> None:
        """A read-only sandbox overrides a write-capable session agent."""
        job = _agent_job(session_agent=AGENT_IMPLEMENTER, sandbox="read-only")
        with (
            patch(f"{_WP}.resolve_agent", return_value="claude"),
            patch(
                f"{_WP}.claude_invoke.invoke_claude_with_session",
                return_value=("out", "sid"),
            ) as invoke,
        ):
            pool.submit(job, StageName.IMPLEMENTATION)
            _handle, result = completion_q.get(timeout=10)
        assert result.ok is True
        assert invoke.call_args.kwargs["allowed_tools"] == "Read,Glob,Grep"
        assert invoke.call_args.kwargs["permission_mode"] == "dontAsk"


def test_sync_checkout_uses_explicit_gh_root_for_api_when_fixed_candidates_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct scope must use ``ROOT/bin/gh`` for its default-branch lookup."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_module

    root = tmp_path / "direct-gh-root"
    executable = root / "bin" / "gh"
    executable.parent.mkdir(parents=True)
    executable.touch()
    executable.chmod(0o755)
    monkeypatch.setattr(worker_pool_module, "_TRUSTED_GH_CANDIDATES", ())

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        gh_extra_path_root=root,
    )
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    expected_executable = str(executable.resolve())
    try:
        with (
            patch("hephaestus.automation.git_utils.run") as mock_run,
            patch.object(
                pool, "_fast_forward_checkout", return_value=JobResult(ok=True)
            ) as fast_forward,
        ):
            mock_run.side_effect = [
                subprocess.CompletedProcess([], 0, stdout="https://github.com/owner/name.git\n"),
                subprocess.CompletedProcess([], 0, stdout=""),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
                subprocess.CompletedProcess([], 0, stdout="main\n"),
            ]
            result = pool._sync_checkout_locked(
                checkout=checkout,
                expected_repo="owner/name",
                timeout_s=120,
            )
    finally:
        pool.shutdown()

    assert result.ok is True
    assert mock_run.call_args_list[3] == call(
        [expected_executable, "api", "repos/owner/name", "--jq", ".default_branch"],
        cwd=checkout,
        timeout=120,
        env=ANY,
    )
    fast_forward.assert_called_once_with(
        checkout=checkout,
        default_branch="main",
        gh_command=expected_executable,
        timeout_s=120,
    )
