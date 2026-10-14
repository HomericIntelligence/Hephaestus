"""Test first publication through the existing source-owned worker."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from hephaestus.agents.workspace import SourceLane
from hephaestus.automation import first_publication_recovery, git_utils
from hephaestus.automation.direct_review_recovery import _write_receipt
from hephaestus.automation.pipeline import worker_pool
from hephaestus.automation.pipeline.git_jobs import GitJob
from hephaestus.automation.pipeline.host_capabilities import WorkerCapabilities
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import _valid_writer_publication_facts
from hephaestus.automation.pipeline.stages.implementation import (
    ImplementationStage,
    _publication_failure_diagnostic,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.pipeline.worker_pool import WorkerPool, _RemoteGitAuthenticationError
from hephaestus.automation.source_worktree import SourceWorkspaceManager
from hephaestus.config.child_environments import build_git_child_env
from hephaestus.utils.helpers import run_subprocess
from tests.unit.automation.pipeline.conftest import FakeSigningProvider
from tests.unit.automation.pipeline.test_worker_pool import _git, _worker_repository
from tests.unit.automation.test_rebase_recovery import (
    _registered_git_fixture,
    _source_registration_fixture,
)

_ACCEPTED_PUSH_FAULTS = frozenset(
    {
        "published",
        "unknown-outcome",
        "completion-before",
        "completion-after",
        "completion-timeout",
        "completion-command",
        "unknown-completion-timeout",
        "unknown-completion-auth",
        "unknown-completion-storage",
    }
)


@pytest.fixture
def protected_creation_mask() -> Iterator[None]:
    """Create owned Git parents without ambient group-write permission."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.mark.parametrize(
    ("require_intent", "fault"),
    [
        pytest.param(False, "transport", id="observation"),
        pytest.param(True, "transport", id="durable-intent"),
        pytest.param(True, "authentication", id="authentication-after-intent"),
        pytest.param(True, "probe-authentication", id="authentication-after-push"),
        pytest.param(True, "push-cancel", id="push-cancellation"),
        pytest.param(True, "probe-cancel", id="probe-cancellation"),
        pytest.param(False, "malformed", id="malformed-candidate"),
        pytest.param(False, "scope", id="source-scope"),
        pytest.param(True, "published", id="published"),
        pytest.param(True, "unknown-outcome", id="push-succeeded-before-error"),
        pytest.param(True, "completion-before", id="completion-write-failed"),
        pytest.param(True, "completion-after", id="completion-written-before-error"),
        pytest.param(True, "completion-timeout", id="completion-probe-timeout"),
        pytest.param(True, "completion-command", id="completion-probe-command"),
        pytest.param(True, "unknown-completion-timeout", id="push-error-then-probe-timeout"),
        pytest.param(True, "unknown-completion-auth", id="push-error-then-completion-auth"),
        pytest.param(True, "unknown-completion-storage", id="push-error-then-storage-failure"),
        pytest.param(True, "recovery-deadline", id="recovery-push-error-then-deadline"),
        pytest.param(
            True, "recovery-completion-deadline", id="recovery-completion-error-then-deadline"
        ),
        pytest.param(True, "recovery-scope-changed", id="recovery-current-scope-changed"),
        pytest.param(True, "recovery-advanced-main", id="recovery-independent-main-advance"),
        pytest.param(True, "recovery-partial-main", id="recovery-partial-upstream-integration"),
        pytest.param(True, "recovery-start-above-main", id="recovery-start-above-main"),
        pytest.param(True, "recovery-hidden-path", id="recovery-path-hidden-by-retained-base"),
        pytest.param(True, "recovery-validation-pass", id="recovery-fresh-validation"),
        pytest.param(True, "recovery-validation-fail", id="recovery-validation-failure"),
        pytest.param(True, "recovery-validation-source", id="recovery-validation-source-change"),
    ],
)
def test_first_publication_failure_distinguishes_confirmed_absence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    require_intent: bool,
    fault: str,
    protected_creation_mask: None,
) -> None:
    """Keep the exact local commit when the remote branch remains absent."""
    root, _, base = _worker_repository(tmp_path)
    _registered_git_fixture(root, monkeypatch)
    _source_registration_fixture(root, monkeypatch)
    manager = SourceWorkspaceManager(
        root, repository="example/project", base_dir=root / "build/.worktrees"
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(None, "unit-worker", FakeSigningProvider()),
    )
    monkeypatch.setattr(
        pool,
        "_authenticated_remote_git_configuration",
        lambda **kwargs: (
            build_git_child_env(),
            ("-c", "protocol.file.allow=always"),
        ),
    )
    try:
        created = pool._run_git(
            GitJob(
                repo="example/project",
                op="create_worktree",
                timeout_s=60,
                kwargs={
                    "issue_number": 9,
                    "branch_name": "writer",
                    "repo_root": str(root),
                    "source_lane": "impl",
                },
            )
        )
        assert created.ok, created.error
        worktree = manager.path_for(9, SourceLane.IMPLEMENTATION)
        original = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        key = tmp_path / "signing-key"
        subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key)],
            check=True,
            capture_output=True,
        )
        commit_command = (
            "-c",
            "user.name=Test User",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "gpg.format=ssh",
            "-c",
            f"user.signingkey={key}",
            "commit",
            "-S",
            "-s",
        )
        intermediate: str | None = None
        with manager.implementation_local_commit(
            9,
            branch="writer",
            path=worktree,
            expected_binding=manager._binding(original),
        ) as record:
            if fault in {
                "recovery-partial-main",
                "recovery-start-above-main",
                "recovery-hidden-path",
            }:
                first_path = "outside.txt" if fault == "recovery-hidden-path" else "local.txt"
                (worktree / first_path).write_text("Starting change.\n", encoding="utf-8")
                _git(worktree, "add", first_path)
                _git(worktree, *commit_command, "-m", "fix: prepare retained start")
                intermediate = _git(worktree, "rev-parse", "HEAD")
                if fault != "recovery-partial-main":
                    base = intermediate
            (worktree / "local.txt").write_text("Local change.\n", encoding="utf-8")
            _git(worktree, "add", "local.txt")
            _git(worktree, *commit_command, "-m", "fix: prepare first publication")
            head = _git(worktree, "rev-parse", "HEAD")
            binding = record(head)
        before = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
        tree = _git(worktree, "rev-parse", "HEAD^{tree}")
        assert _git(worktree, "ls-remote", "origin", "refs/heads/writer") == ""

        evidence = _PublicationEvidence()
        push_calls = evidence.push_calls
        absent_reads = evidence.absent_reads
        malformed: Path | None = None
        if fault in {"completion-before", "completion-after", "unknown-completion-storage"}:
            _fail_completion_write(monkeypatch, after_write=fault == "completion-after")

        if fault in {"authentication", "probe-authentication", "unknown-completion-auth"}:

            def authenticate(**kwargs: Any) -> tuple[dict[str, str], tuple[str, ...]]:
                if fault == "unknown-completion-auth" and evidence.remote_reads_after_push:
                    raise _RemoteGitAuthenticationError(
                        "controlled completion authentication failure"
                    )
                fail = (
                    bool(push_calls)
                    if fault == "probe-authentication"
                    else bool(list((manager.state_dir / "first-publications").glob("9-*.json")))
                )
                if fail and fault != "unknown-completion-auth":
                    raise _RemoteGitAuthenticationError("controlled authentication failure")
                return build_git_child_env(), ("-c", "protocol.file.allow=always")

            monkeypatch.setattr(pool, "_authenticated_remote_git_configuration", authenticate)
        if fault == "malformed":
            namespace = manager.state_dir / "first-publications"
            namespace.mkdir(mode=0o700)
            malformed = namespace / f"9-{'0' * 32}.json"
            malformed.write_text("{}", encoding="utf-8")
            malformed.chmod(0o600)

        original_transport = git_utils.run
        _install_publication_transport(
            monkeypatch, manager, evidence, fault, require_intent, base, tree, head
        )
        job = GitJob(
            repo="example/project",
            op="commit_push",
            workspace=binding,
            timeout_s=60,
            kwargs={
                "issue_number": 9,
                "repo_root": str(root),
                "source_lane": "impl",
                "worktree_path": str(worktree),
                "branch": "writer",
                "agent": "codex",
                "allowed_paths": ("local.txt",),
                "scope_history_base_sha": base,
                "publish_base_sha": base,
            },
        )
        if fault == "scope":
            job.kwargs["allowed_paths"] = ("different.txt",)
            with manager.implementation_local_commit(
                9, branch="writer", path=worktree, expected_binding=binding
            ):
                result = pool._publish_first_writer(job, "writer", worktree, head)
        else:
            result = pool._run_git(job)
        assert result.ok is (fault in {"published", "unknown-outcome"})
        assert _git(worktree, "rev-parse", "HEAD") == head
        assert _git(worktree, "rev-parse", "HEAD^{tree}") == tree
        assert _git(worktree, "status", "--porcelain") == ""
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == before
        _assert_publication_failure(
            fault,
            result,
            manager,
            push_calls,
            absent_reads,
            evidence.retained_intent,
            require_intent,
            head,
            malformed,
        )
        if require_intent and (
            fault in _ACCEPTED_PUSH_FAULTS or fault == "transport" or fault.startswith("recovery-")
        ):
            monkeypatch.setattr(first_publication_recovery, "_write_receipt", _write_receipt)
            _advance_recovery_main(root, fault, intermediate)
            _assert_fresh_worker_discovers_publication(
                tmp_path, monkeypatch, manager, evidence, head, original_transport, fault
            )
    finally:
        pool.shutdown()


def _advance_recovery_main(root: Path, fault: str, intermediate: str | None) -> None:
    """Advance main independently or integrate part of the retained work."""
    if fault == "recovery-advanced-main":
        (root / "upstream.txt").write_text("Independent upstream change.\n", encoding="utf-8")
        _git(root, "add", "upstream.txt")
        _git(root, "commit", "-m", "fix: advance independent main")
        _git(root, "push", "origin", "main")
    elif fault == "recovery-partial-main":
        assert intermediate is not None
        _git(root, "merge", "--ff-only", intermediate)
        _git(root, "push", "origin", "main")


def _assert_fresh_worker_discovers_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    manager: SourceWorkspaceManager,
    evidence: _PublicationEvidence,
    head: str,
    original_transport: Any,
    recovery_case: str,
) -> None:
    """Discover retained intent and completion without a caller-supplied source."""
    records = list((manager.state_dir / "first-publications").glob("9-*.json"))
    assert len(records) == 1
    content = records[0].read_bytes()
    record = json.loads(content)
    receipt = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
    before_pushes = list(evidence.push_calls)
    restarted = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(None, "unit-worker", FakeSigningProvider()),
    )
    monkeypatch.setattr(
        restarted,
        "_authenticated_remote_git_configuration",
        lambda **kwargs: (build_git_child_env(), ("-c", "protocol.file.allow=always")),
    )
    try:
        job = GitJob(
            repo="example/project",
            op="discover_first_publication",
            timeout_s=60,
            kwargs={
                "repo_root": str(manager.repo_root),
                "issue_number": 9,
                "branch": "",
                "publication_discovery_request_id": "d" * 32,
            },
        )
        result = restarted._run_git(job)
        assert result.ok, result.error
        assert result.value == {
            "publication_discovery_request_id": "d" * 32,
            "repository": "example/project",
            "issue_number": 9,
            "first_publication_candidate": record["operation_id"],
            "source_workspace": manager._binding(receipt).to_dict(),
            "source_receipt": receipt.to_dict(),
        }
        other_branch = restarted._run_git(
            replace(job, kwargs={**job.kwargs, "branch": "different-writer"})
        )
        assert not other_branch.ok
        assert other_branch.error is not None
        assert "current source" in other_branch.error
        receipt_path = manager._receipt_path(9, SourceLane.IMPLEMENTATION)
        retained_receipt = receipt_path.with_suffix(".test-backup")
        receipt_path.rename(retained_receipt)
        try:
            missing_source = restarted._run_git(job)
            assert not missing_source.ok
            assert missing_source.error is not None
            assert "receipt does not exist" in missing_source.error
        finally:
            retained_receipt.rename(receipt_path)
        assert records[0].read_bytes() == content
        assert evidence.push_calls == before_pushes
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == head
        assert _git(receipt.path, "status", "--porcelain") == ""
        monkeypatch.setattr(git_utils, "run", original_transport)
        remote_before_recovery = _git(receipt.path, "ls-remote", "origin", "refs/heads/writer")
        assert remote_before_recovery in {"", f"{head}\trefs/heads/writer"}
        resumed_evidence = _PublicationEvidence()
        _install_publication_transport(
            monkeypatch,
            manager,
            resumed_evidence,
            {
                "recovery-deadline": "unknown-outcome",
                "recovery-completion-deadline": "unknown-completion-timeout",
            }.get(recovery_case, "published"),
            True,
            record["scope_base_sha"],
            record["tree_sha"],
            head,
        )
        if recovery_case == "recovery-deadline":
            _expire_after_recovery_reconciliation(monkeypatch, resumed_evidence)
        elif recovery_case == "recovery-completion-deadline":
            _expire_after_completion_failure(monkeypatch)
        retained_identity = records[0].stat()
        validation_calls = _install_recovery_validation(monkeypatch, receipt.path, recovery_case)
        recovery_job = GitJob(
            repo="example/project",
            op="commit_push",
            workspace=manager._binding(receipt),
            timeout_s=60,
            kwargs={
                "repo_root": str(manager.repo_root),
                "issue_number": 9,
                "worktree_path": str(receipt.path),
                "source_lane": "impl",
                "branch": "writer",
                "agent": "codex",
                "allowed_paths": (
                    ("different.txt",)
                    if recovery_case == "recovery-scope-changed"
                    else ("local.txt",)
                ),
                "scope_history_base_sha": head,
                "publish_base_sha": head,
                "first_publication_candidate": record["operation_id"],
                "publication_recovery_request_id": "e" * 32,
                "publication_test_argv": (
                    ("controlled", "focused")
                    if recovery_case.startswith("recovery-validation-")
                    else None
                ),
            },
        )
        recovered = restarted._run_git(recovery_job)
        if recovery_case.startswith("recovery-validation-"):
            assert validation_calls == [("controlled", "focused")]
            if recovery_case != "recovery-validation-pass":
                assert not recovered.ok
                assert not resumed_evidence.push_calls
                assert records[0].read_bytes() == content
                assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
                assert _git(receipt.path, "rev-parse", "HEAD") == head
                assert "focused validation stderr" in recovered.stderr_tail
                return
        if recovery_case in {
            "recovery-deadline",
            "recovery-completion-deadline",
            "recovery-scope-changed",
            "recovery-hidden-path",
        }:
            assert not recovered.ok
            assert records[0].read_bytes() == content
            if recovery_case in {"recovery-deadline", "recovery-completion-deadline"}:
                assert recovered.error == "timeout"
                assert "Controlled publication transport failure." in recovered.stderr_tail
                assert recovered.process_failure is not None
                assert (
                    recovered.process_failure.exception_class == "SourceWorkspacePreparationError"
                )
                if recovery_case == "recovery-completion-deadline":
                    assert len(resumed_evidence.push_calls) == 1
                    assert resumed_evidence.remote_reads_after_push == 2
                    assert "completion probe stdout" in recovered.stdout_tail
                    assert "completion probe timeout" in recovered.stderr_tail
                    assert recovered.stderr_tail.index("Controlled publication") < (
                        recovered.stderr_tail.index("completion probe timeout")
                    )
            else:
                assert not resumed_evidence.push_calls
                assert recovered.error is not None and "scope" in recovered.error
            assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
            assert _git(receipt.path, "rev-parse", "HEAD") == head
            return
        assert recovered.ok, recovered.error
        assert isinstance(recovered.value, dict)
        assert recovered.value["head_sha"] == head
        assert recovered.value["publication_recovery_request_id"] == "e" * 32
        callback_item = WorkItem(
            repo=recovery_job.repo,
            kind=ItemKind.ISSUE,
            issue=9,
            state="GATE",
            branch="writer",
            worktree=str(receipt.path),
            payload={
                "_impl_source_workspace": manager._binding(receipt).to_dict(),
                "_impl_source_revision": head,
                "_impl_source_receipt": receipt,
            },
        )
        assert _valid_writer_publication_facts(
            ImplementationStage._validate_first_publication_result(
                callback_item, recovery_job, recovered
            )
        )
        assert _git(receipt.path, "ls-remote", "origin", "refs/heads/writer") == (
            f"{head}\trefs/heads/writer"
        )
        assert len(resumed_evidence.push_calls) == (remote_before_recovery == "")
        if record["phase"] == "complete":
            assert records[0].read_bytes() == content
            assert records[0].stat().st_ino == retained_identity.st_ino
            assert records[0].stat().st_mtime_ns == retained_identity.st_mtime_ns
            _assert_completed_record_rechecked(monkeypatch, restarted, recovery_job, records[0])
        else:
            assert json.loads(records[0].read_bytes())["phase"] == "complete"
        assert manager._require_receipt(9, SourceLane.IMPLEMENTATION) == receipt
        assert _git(receipt.path, "rev-parse", "HEAD") == head
        assert _git(receipt.path, "status", "--porcelain") == ""
    finally:
        restarted.shutdown()


def _install_recovery_validation(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, recovery_case: str
) -> list[tuple[str, ...]]:
    """Control the validation subprocess without replacing its worker owner."""
    calls: list[tuple[str, ...]] = []

    def execute(argv: Any, **kwargs: Any) -> Any:
        if tuple(argv) != ("controlled", "focused"):
            return run_subprocess(argv, **kwargs)
        calls.append(tuple(argv))
        assert Path(kwargs["cwd"]) == worktree
        if recovery_case == "recovery-validation-source":
            (worktree / "local.txt").write_text("Changed during validation.\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            argv,
            1 if recovery_case == "recovery-validation-fail" else 0,
            stdout="focused validation stdout",
            stderr="focused validation stderr",
        )

    monkeypatch.setattr(worker_pool, "run_subprocess", execute)
    return calls


def _expire_after_completion_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expire the operation only after the completion probe fails."""
    run = git_utils.run
    monotonic = time.monotonic
    expired = False

    def transport(argv: Any, **kwargs: Any) -> Any:
        nonlocal expired
        try:
            return run(argv, **kwargs)
        except subprocess.TimeoutExpired:
            expired = True
            raise

    monkeypatch.setattr(git_utils, "run", transport)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + (120 if expired else 0))


def _assert_completed_record_rechecked(
    monkeypatch: pytest.MonkeyPatch, pool: WorkerPool, job: GitJob, record_path: Path
) -> None:
    """Reject a changed completed operation after the remote observation."""
    run = git_utils.run
    original = record_path.read_bytes()
    damaged = b'{"changed": true}\n'

    def transport(argv: Any, **kwargs: Any) -> Any:
        result = run(argv, **kwargs)
        if "ls-remote" in argv and argv[-1] == "refs/heads/writer":
            record_path.write_bytes(damaged)
        return result

    try:
        with monkeypatch.context() as changes:
            changes.setattr(git_utils, "run", transport)
            result = pool._run_git(
                replace(job, kwargs={**job.kwargs, "publication_recovery_request_id": "f" * 32})
            )
        assert not result.ok, "Recovery must recheck its operation after the remote observation"
        assert record_path.read_bytes() == damaged
    finally:
        record_path.write_bytes(original)


def _expire_after_recovery_reconciliation(
    monkeypatch: pytest.MonkeyPatch, evidence: _PublicationEvidence
) -> None:
    """Advance the controlled clock only after the retry result is reconciled."""
    run = git_utils.run
    monotonic = time.monotonic
    expired = False

    def transport(argv: Any, **kwargs: Any) -> Any:
        nonlocal expired
        result = run(argv, **kwargs)
        if "ls-remote" in argv and evidence.push_calls:
            expired = True
        return result

    monkeypatch.setattr(git_utils, "run", transport)
    monkeypatch.setattr(time, "monotonic", lambda: monotonic() + (120 if expired else 0))


@dataclass
class _PublicationEvidence:
    """Keep observations from the controlled transport and real storage."""

    push_calls: list[tuple[str, ...]] = field(default_factory=list)
    absent_reads: list[tuple[str, ...]] = field(default_factory=list)
    retained_intent: tuple[Path, bytes] | None = None
    remote_reads_after_push: int = 0


def test_discovery_authentication_failure_retains_request_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected_creation_mask: None
) -> None:
    """A failure before lock admission must still identify its discovery callback."""
    root, _, base = _worker_repository(tmp_path)
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_capabilities=WorkerCapabilities(None, "unit-worker", FakeSigningProvider()),
    )

    def unavailable(**kwargs: Any) -> tuple[dict[str, str], tuple[str, ...]]:
        raise _RemoteGitAuthenticationError("controlled discovery authentication failure")

    monkeypatch.setattr(pool, "_authenticated_remote_git_configuration", unavailable)
    try:
        result = pool._run_git(
            GitJob(
                repo="example/project",
                op="discover_first_publication",
                timeout_s=60,
                kwargs={
                    "repo_root": str(root),
                    "issue_number": 9,
                    "branch": "",
                    "publication_discovery_request_id": "d" * 32,
                },
            )
        )
        assert not result.ok
        assert isinstance(result.value, dict)
        assert result.value.get("failure_kind") == "remote_authentication"
        assert result.value.get("publication_discovery_request_id") == "d" * 32
        assert result.value.get("repository") == "example/project"
        assert result.value.get("issue_number") == 9
        assert _git(root, "rev-parse", "HEAD") == base
    finally:
        pool.shutdown()


def _install_publication_transport(
    monkeypatch: pytest.MonkeyPatch,
    manager: SourceWorkspaceManager,
    evidence: _PublicationEvidence,
    fault: str,
    require_intent: bool,
    base: str,
    tree: str,
    head: str,
) -> None:
    """Inject faults only at transport boundaries; use the real Git repository."""
    run = git_utils.run

    def transport(argv: Any, **kwargs: Any) -> Any:
        command = tuple(argv)
        if "push" in command:
            evidence.push_calls.append(command)
            if require_intent:
                evidence.retained_intent = _assert_durable_intent(manager, command, base, tree)
            if fault == "push-cancel":
                raise InterruptedError("controlled push cancellation")
            if fault in _ACCEPTED_PUSH_FAULTS:
                pushed = run(argv, **kwargs)
                if not fault.startswith("unknown-"):
                    return pushed
            raise subprocess.CalledProcessError(
                1, command, stderr="Controlled publication transport failure."
            )
        if "ls-remote" in command and evidence.push_calls and fault == "probe-cancel":
            raise InterruptedError("controlled probe cancellation")
        if "ls-remote" in command and evidence.push_calls:
            evidence.remote_reads_after_push += 1
            _fail_completion_probe(fault, evidence.remote_reads_after_push, command)
        result = run(argv, **kwargs)
        if "ls-remote" in command and command[-1] == "refs/heads/writer":
            assert result.returncode == 0
            if result.stdout.strip():
                assert result.stdout.strip() == f"{head}\trefs/heads/writer"
            else:
                evidence.absent_reads.append(command)
        return result

    monkeypatch.setattr(git_utils, "run", transport)


def _fail_completion_probe(fault: str, read_number: int, command: tuple[str, ...]) -> None:
    """Let reconciliation succeed before a separate completion observation fails."""
    if fault.startswith("unknown-") and read_number == 1:
        return
    if fault in {"completion-timeout", "unknown-completion-timeout"}:
        raise subprocess.TimeoutExpired(
            command, 17, output="completion probe stdout", stderr="completion probe timeout"
        )
    if fault == "completion-command":
        raise subprocess.CalledProcessError(
            128, command, output="completion probe stdout", stderr="completion probe failed"
        )


def _assert_durable_intent(
    manager: SourceWorkspaceManager, command: tuple[str, ...], base: str, tree: str
) -> tuple[Path, bytes]:
    """Bind actual intent bytes to the current source before any push."""
    records = list((manager.state_dir / "first-publications").glob("9-*.json"))
    assert len(records) == 1, "Publication requires one durable operation intent"
    content = records[0].read_bytes()
    intent = json.loads(content)
    assert intent["schema_version"] == 1
    assert intent["phase"] == "publication_intent"
    assert intent["expected_remote"] == "absent"
    assert intent["repository"] == "example/project"
    assert intent["scheduler_repository"] == "example/project"
    assert intent["issue_number"] == 9
    assert intent["branch"] == "writer"
    assert intent["destination"] == "https://github.com/example/project.git"
    current = manager._require_receipt(9, SourceLane.IMPLEMENTATION)
    assert intent["workspace"] == manager._binding(current).to_dict()
    assert intent["tree_sha"] == tree
    assert intent["scope_base_sha"] == base
    assert intent["allowed_paths"] == ["local.txt"]
    assert records[0].name == f"9-{intent['operation_id']}.json"
    assert "--no-verify" not in command
    assert "--force-with-lease=refs/heads/writer:" in command
    assert not any("core.hookspath=" in part.lower() for part in command)
    return records[0], content


def _assert_publication_failure(
    fault: str,
    result: JobResult,
    manager: SourceWorkspaceManager,
    push_calls: list[tuple[str, ...]],
    absent_reads: list[tuple[str, ...]],
    retained_intent: tuple[Path, bytes] | None,
    require_intent: bool,
    head: str,
    malformed: Path | None,
) -> None:
    """Check the distinct failure result and its retained disk evidence."""
    if fault in {
        "completion-timeout",
        "completion-command",
        "unknown-completion-timeout",
        "unknown-completion-auth",
        "unknown-completion-storage",
    }:
        assert len(push_calls) == 1
        assert absent_reads
        assert retained_intent is not None
        assert retained_intent[0].read_bytes() == retained_intent[1]
        _assert_completion_failure_evidence(fault, result)
        assert (
            _git(
                manager.path_for(9, SourceLane.IMPLEMENTATION),
                "ls-remote",
                "origin",
                "refs/heads/writer",
            ).strip()
            == f"{head}\trefs/heads/writer"
        )
        return
    if fault in {"published", "unknown-outcome", "completion-before", "completion-after"}:
        assert len(push_calls) == 1
        assert absent_reads
        assert retained_intent is not None
        phase = json.loads(retained_intent[0].read_bytes())["phase"]
        if fault in {"completion-before", "completion-after"}:
            assert result.value == {"first_publication_failure": "admission_or_storage"}
            assert phase == ("complete" if fault == "completion-after" else "publication_intent")
        else:
            assert phase == "complete"
            assert result.value["pushed"] is True
            assert result.value["head_sha"] == head
        assert (
            _git(
                manager.path_for(9, SourceLane.IMPLEMENTATION),
                "ls-remote",
                "origin",
                "refs/heads/writer",
            ).strip()
            == f"{head}\trefs/heads/writer"
        )
        return
    if fault == "scope":
        assert not push_calls
        assert not absent_reads
        assert result.error == "implementation changed paths outside approved scope"
        assert result.value is None
        assert not (manager.state_dir / "first-publications").exists()
        return
    if fault == "malformed":
        assert not push_calls
        assert not absent_reads
        assert result.value == {"first_publication_failure": "admission_or_storage"}
        assert malformed is not None
        assert malformed.read_text(encoding="utf-8") == "{}"
        return
    assert absent_reads
    if fault in {"authentication", "probe-authentication"}:
        assert len(push_calls) == int(fault == "probe-authentication")
        assert result.value == {"failure_kind": "remote_authentication"}
        records = list((manager.state_dir / "first-publications").glob("9-*.json"))
        assert len(records) == 1
        assert json.loads(records[0].read_bytes())["phase"] == "publication_intent"
        if fault == "probe-authentication":
            assert result.process_failure is not None
            assert result.process_failure.returncode == 1
            assert "Controlled publication transport failure." in result.stderr_tail
        return
    assert len(push_calls) == 1
    if fault in {"push-cancel", "probe-cancel"}:
        assert result.interrupted
        assert retained_intent is not None
        assert retained_intent[0].read_bytes() == retained_intent[1]
        return
    assert isinstance(result.value, dict)
    assert result.value["head_sha"] == head
    assert result.value["baseline_remote_sha"] is None
    assert result.value["observed_remote_sha"] is None
    assert result.value["publication_state"] == "remote_absent"
    assert result.value["pushed"] is False
    if require_intent:
        assert retained_intent is not None
        assert retained_intent[0].read_bytes() == retained_intent[1]


def _assert_completion_failure_evidence(fault: str, result: JobResult) -> None:
    """Require original push evidence plus the precise later failure cause."""
    assert result.error is not None
    if fault == "unknown-completion-auth":
        assert result.value == {"failure_kind": "remote_authentication"}
        assert "controlled completion authentication failure" in result.error
    elif fault == "unknown-completion-storage":
        assert result.value == {"first_publication_failure": "admission_or_storage"}
        assert "controlled completion write failure" in result.error
    else:
        assert "completion probe stdout" in result.stdout_tail
        assert result.process_failure is not None
        if fault.endswith("timeout"):
            assert result.process_failure.exception_class == "TimeoutExpired"
            assert "completion probe timeout" in result.stderr_tail
        else:
            assert result.process_failure.returncode == 128
            assert "completion probe failed" in result.stderr_tail
    if fault.startswith("unknown-"):
        assert "Controlled publication transport failure." in result.stderr_tail


def _fail_completion_write(monkeypatch: pytest.MonkeyPatch, *, after_write: bool) -> None:
    """Fail at the real completion write boundary without changing its intent."""

    def write(*args: Any, **kwargs: Any) -> None:
        complete = json.loads(args[-1])["phase"] == "complete"
        if not complete or after_write:
            _write_receipt(*args, **kwargs)
        if complete:
            raise OSError("controlled completion write failure")

    monkeypatch.setattr(first_publication_recovery, "_write_receipt", write)


@pytest.mark.parametrize(
    ("baseline", "observed", "refresh", "valid"),
    [
        (None, None, None, True),
        ("a" * 40, None, None, False),
        (None, "b" * 40, None, False),
        (None, None, "publish", False),
    ],
)
def test_absent_publication_facts_exclude_existing_head_refresh(
    baseline: str | None, observed: str | None, refresh: str | None, valid: bool
) -> None:
    """Accept absence facts only for an unpublished ordinary writer."""
    assert (
        _valid_writer_publication_facts(
            {
                "publication_state": "remote_absent",
                "head_sha": "c" * 40,
                "baseline_remote_sha": baseline,
                "observed_remote_sha": observed,
                "pushed": False,
                "refresh_phase": refresh,
            }
        )
        is valid
    )


def test_absent_publication_retains_structured_failure_diagnostics() -> None:
    """Report confirmed absence without losing the publication error tails."""
    result = JobResult(
        ok=False,
        value={
            "publication_state": "remote_absent",
            "head_sha": "c" * 40,
            "baseline_remote_sha": None,
            "observed_remote_sha": None,
            "pushed": False,
            "refresh_phase": None,
        },
        stderr_tail="Controlled publication transport failure.",
    )
    diagnostic = _publication_failure_diagnostic(result)
    assert diagnostic is not None
    assert diagnostic["phase"] == "push"
    assert diagnostic["remote_state"] == "absent"
    assert diagnostic["head_sha"] == "c" * 40
    assert diagnostic["stderr_tail"] == result.stderr_tail


@pytest.mark.parametrize("output", ["", "\n", "malformed\n"])
def test_existing_head_reader_does_not_accept_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    """A successful empty read cannot supply an existing-head lease."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    monkeypatch.setattr(pool, "_authenticated_remote_git_configuration", lambda **kwargs: ({}, ()))

    def remote_read(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["git", "ls-remote", "--refs", "origin", "refs/heads/writer"]
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(git_utils, "run", remote_read)
    try:
        result = pool._read_remote_branch_head(
            tmp_path, remote="origin", branch="writer", expected_repo="example/project", timeout=60
        )
        assert isinstance(result, JobResult)
        assert not result.ok
        observed = pool._read_remote_branch_state(
            tmp_path, remote="origin", branch="writer", expected_repo="example/project", timeout=60
        )
        if output == "":
            assert observed is None
        else:
            assert isinstance(observed, JobResult)
            assert not observed.ok
    finally:
        pool.shutdown()
