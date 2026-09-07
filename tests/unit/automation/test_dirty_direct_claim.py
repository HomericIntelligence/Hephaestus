"""Tests for durable one-use dirty writer claims."""

from dataclasses import replace
from pathlib import Path

import pytest

from hephaestus.agents.workspace import DirtyPlanIdentity, SourceLane
from hephaestus.automation.source_worktree import SourceWorkspaceError, SourceWorkspaceManager
from hephaestus.automation.worktree_snapshot import _dirty_worktree_content_snapshot
from hephaestus.config.child_environments import build_git_child_env
from tests.unit.agents.test_dirty_workspace import _claim
from tests.unit.automation.test_source_worktree import _repository


def test_dirty_claim_consumes_before_turn_and_rejects_replay(tmp_path: Path) -> None:
    """A failed writer turn leaves a consumed receipt and all pending content."""
    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(
        12, claim=claim, expected_generation=original.generation
    )
    identity = DirtyPlanIdentity(
        claim.plan_revision, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with pytest.raises(RuntimeError, match="provider failed"):
        with manager.acquire(binding, dirty_plan_identity=identity):
            receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
            assert receipt.dirty_claim is not None
            assert receipt.dirty_claim.state == "consumed"
            raise RuntimeError("provider failed")
    with pytest.raises(SourceWorkspaceError):
        with manager.acquire(binding, dirty_plan_identity=identity):
            pytest.fail("a consumed turn ran again")
    assert (original.cwd / "tracked.txt").read_text() == "pending\n"


@pytest.mark.parametrize(
    "change", ["plan", "review", "scope", "generation", "content", "issue", "repository"]
)
def test_dirty_job_rejects_changed_inputs_before_turn(tmp_path: Path, change: str) -> None:
    """Independent frozen inputs and live ownership must match the claim."""
    import hashlib

    from hephaestus.automation.pipeline.jobs import AgentJob, DirtyDirectPlanInput
    from hephaestus.automation.pipeline.worker_pool import _agent_workspace_lease
    from hephaestus.automation.review_journal import plan_fingerprint

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    plan = "## Files to Modify\n- `tracked.txt`\n"
    review = "The plan is complete.\nstate:plan-go"
    claim = replace(
        _claim(),
        reservation_base_sha=head,
        plan_fingerprint=plan_fingerprint(plan),
        review_fingerprint=hashlib.sha256(review.encode()).hexdigest(),
    )
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(
        12, claim=claim, expected_generation=original.generation
    )
    inputs = DirtyDirectPlanInput(5, plan, 5, review, ("tracked.txt",))
    if change == "plan":
        inputs = replace(inputs, plan=plan + "Changed behavior.\n")
    elif change == "review":
        inputs = replace(inputs, review="Different approval.\nstate:plan-go")
    elif change == "scope":
        inputs = replace(inputs, allowed_paths=("other.txt",))
    elif change == "generation":
        binding = replace(binding, generation=binding.generation + 1)
    elif change == "content":
        (original.cwd / "tracked.txt").write_text("changed after claim\n")
    job = AgentJob(
        repo="project",
        issue=12,
        agent="codex",
        model="gpt-6-astra:low",
        prompt_builder=lambda: "prompt",
        cwd=original.cwd,
        timeout_s=30,
        workspace=binding,
        retryable=False,
        dirty_plan=inputs,
    )
    if change == "issue":
        job = replace(job, issue=13)
    elif change == "repository":
        job = replace(job, repo="foreign")
    with pytest.raises(SourceWorkspaceError):
        with _agent_workspace_lease(job):
            pytest.fail("changed inputs reached the provider turn")
    stored = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
    assert stored.dirty_claim is not None and stored.dirty_claim.state == "armed"


@pytest.mark.parametrize(
    "failure", [None, "owner", "revision", "label", "review", "missing", "skip", "blocked"]
)
def test_fresh_plan_read_requires_owned_matching_approval(failure: str | None) -> None:
    """Neither approval prose nor a stale review can authorize a dirty claim."""
    from dataclasses import asdict

    from hephaestus.automation.pipeline.github_jobs import DirtyDirectPrStateRead, FrozenJson
    from hephaestus.automation.pipeline.worker_pool import _dirty_plan_from_read
    from hephaestus.automation.review_journal import (
        IssueComment,
        render_current_plan,
        render_current_review,
    )

    plan = "## Files to Modify\n- `tracked.txt`\n"
    review = "The plan is complete.\nstate:plan-go"
    comments = [
        IssueComment(render_current_plan(plan, revision=5), viewer_did_author=failure != "owner"),
        IssueComment(
            render_current_review(
                review if failure != "review" else "The plan is approved.",
                revision=4 if failure == "revision" else 5,
            ),
            viewer_did_author=True,
        ),
    ]
    receipt = DirtyDirectPrStateRead(
        "example/project",
        12,
        _claim().branch,
        (),
        None,
        plan_journal=None
        if failure == "missing"
        else FrozenJson.snapshot([asdict(c) for c in comments]),
        issue_state="OPEN",
        issue_labels=("state:plan-no-go",)
        if failure == "label"
        else (
            ("state:plan-go", f"state:{failure}")
            if failure in {"skip", "blocked"}
            else ("state:plan-go",)
        ),
    )
    if failure is None:
        assert _dirty_plan_from_read(receipt).allowed_paths == ("tracked.txt",)
    else:
        with pytest.raises(SourceWorkspaceError):
            _dirty_plan_from_read(receipt)


def test_claim_git_operation_keeps_original_direct_branch(tmp_path: Path) -> None:
    """A dirty restart claims its owned branch without another reservation."""
    import queue
    import threading
    from dataclasses import asdict
    from unittest.mock import MagicMock, patch

    from hephaestus.automation.pipeline.github_jobs import DirtyDirectPrStateRead, FrozenJson
    from hephaestus.automation.pipeline.jobs import GitJob
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from hephaestus.automation.review_journal import (
        IssueComment,
        render_current_plan,
        render_current_review,
    )

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    branch = _claim().branch
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    comments = [
        IssueComment(
            render_current_plan("## Files to Modify\n- `tracked.txt`", revision=5),
            viewer_did_author=True,
        ),
        IssueComment(render_current_review("state:plan-go", revision=5), viewer_did_author=True),
    ]
    runner = MagicMock()
    runner.run.return_value = DirtyDirectPrStateRead(
        "example/project",
        12,
        branch,
        (),
        None,
        plan_journal=FrozenJson.snapshot([asdict(c) for c in comments]),
        issue_state="OPEN",
        issue_labels=("state:plan-go",),
    )
    pool = WorkerPool(1, threading.Event(), queue.Queue(), github_job_runner=runner)
    try:
        with patch.object(pool, "_read_remote_branch_head", return_value=head):
            result = pool._run_git(
                GitJob(
                    repo="project",
                    op="claim_dirty_direct_continuation",
                    timeout_s=30,
                    expected_repository="example/project",
                    kwargs={"repo_root": str(repo), "issue_number": 12},
                )
            )
        assert result.ok, result.error
        assert result.value["branch"] == branch
        assert result.value["source_workspace"]["schema_version"] == 2
        assert manager._require_receipt(12, SourceLane.IMPLEMENTATION).dirty_claim is not None
        assert runner.run.call_count == 1
    finally:
        pool._executor.shutdown()


def test_consumed_dirty_binding_cannot_be_armed_again(tmp_path: Path) -> None:
    """A consumed claim does not regain authority after its content is restored."""
    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(12, claim=claim, expected_generation=1)
    identity = DirtyPlanIdentity(
        5, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    with pytest.raises(SourceWorkspaceError):
        manager.claim_dirty_direct_continuation(
            12, claim=claim, expected_generation=binding.generation
        )


def test_dirty_publication_records_controlled_commit_before_push(tmp_path: Path) -> None:
    """A failed push retains the exact local commit in the consumed receipt."""
    from tests.unit.automation.test_source_worktree import _git

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="example/project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(12, claim=claim, expected_generation=1)
    identity = DirtyPlanIdentity(
        5, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    with manager.dirty_direct_publication(binding) as advance:
        _git(original.cwd, "commit", "-am", "controlled test commit")
        new_head = _git(original.cwd, "rev-parse", "HEAD")
        advance(new_head)
    receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
    assert receipt.revision == new_head
    assert receipt.generation == binding.generation + 1
    assert receipt.dirty_claim is not None and receipt.dirty_claim.state == "consumed"
    assert "dirty-direct-continuation" in receipt.obligations
    manager.finish_dirty_direct_publication(12, expected_head=new_head, pr_number=17)
    completed = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
    assert completed.schema_version == 1
    assert completed.revision == new_head
    assert completed.dirty_claim is None
    assert "dirty-direct-continuation" not in completed.obligations
    with pytest.raises(SourceWorkspaceError):
        with manager.acquire(binding, dirty_plan_identity=identity):
            pytest.fail("publication gave another writer turn")


def test_dirty_publication_missing_runner_preserves_before_stage(tmp_path: Path) -> None:
    """Publication cannot stage content without fresh locked PR evidence."""
    import queue
    import threading
    from unittest.mock import patch

    from hephaestus.automation.pipeline.jobs import GitJob
    from hephaestus.automation.pipeline.worker_pool import WorkerPool

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(12, claim=claim, expected_generation=1)
    identity = DirtyPlanIdentity(
        5, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    pool = WorkerPool(1, threading.Event(), queue.Queue())
    try:
        with patch("hephaestus.automation.commit_runtime._stage_commit_paths") as stage:
            result = pool._run_git(
                GitJob(
                    repo="project",
                    op="publish_dirty_direct_continuation",
                    timeout_s=30,
                    expected_repository="example/project",
                    kwargs={
                        "repo_root": str(repo),
                        "issue_number": 12,
                        "source_workspace": binding.to_dict(),
                    },
                )
            )
        assert not result.ok
        assert result.value["phase"] == "pre_stage"
        assert result.value["committed"] is False
        stage.assert_not_called()
        assert _dirty_worktree_content_snapshot(original.cwd, timeout=10) == snapshot
    finally:
        pool._executor.shutdown()


def test_failed_commit_helper_cannot_advance_dirty_receipt(tmp_path: Path) -> None:
    """A failed helper cannot authorize a changed physical HEAD."""
    import queue
    import threading
    from unittest.mock import patch

    from hephaestus.automation.pipeline.jobs import GitJob
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(12, claim=claim, expected_generation=1)
    identity = DirtyPlanIdentity(
        5, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    pool = WorkerPool(1, threading.Event(), queue.Queue())
    try:

        def fail_after_commit(*args: object, **kwargs: object) -> None:
            _git(original.cwd, "add", "tracked.txt")
            _git(original.cwd, "commit", "-m", "untrusted child")
            raise RuntimeError("commit helper failed")

        with (
            patch.object(pool, "_verify_dirty_direct_plan"),
            patch.object(pool, "_read_remote_branch_head", return_value=head),
            patch.object(pool, "_verify_implementation_edit_scope", return_value=None),
            patch(
                "hephaestus.automation.pipeline.worker_pool._controlled_git_signing_env",
                return_value=build_git_child_env(),
            ),
            patch(
                "hephaestus.automation.commit_runtime._commit_with_signature",
                side_effect=fail_after_commit,
            ),
        ):
            result = pool._run_git(
                GitJob(
                    repo="project",
                    op="publish_dirty_direct_continuation",
                    timeout_s=30,
                    expected_repository="example/project",
                    kwargs={
                        "repo_root": str(repo),
                        "issue_number": 12,
                        "source_workspace": binding.to_dict(),
                    },
                )
            )
        assert not result.ok
        assert result.value["phase"] == "pre_stage"
        assert _git(original.cwd, "rev-parse", "HEAD") != head
        receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert receipt.revision == head
        assert receipt.generation == binding.generation
        assert receipt.dirty_claim is not None and receipt.dirty_claim.state == "consumed"
    finally:
        pool._executor.shutdown()


@pytest.mark.parametrize(
    "outcome", ["success", "remote", "scope", "no_changes", "plan_race", "push_race"]
)
def test_dirty_publication_preserves_exact_scope_and_lease(tmp_path: Path, outcome: str) -> None:
    """Publication verifies scope and current evidence before its exact lease push."""
    import queue
    import threading
    from unittest.mock import patch

    from hephaestus.automation.pipeline.job_results import JobResult
    from hephaestus.automation.pipeline.jobs import GitJob
    from hephaestus.automation.pipeline.worker_pool import WorkerPool
    from tests.unit.automation.test_source_worktree import _git

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    claim = replace(_claim(), reservation_base_sha=head)
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    (original.cwd / "tracked.txt").write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(12, claim=claim, expected_generation=1)
    identity = DirtyPlanIdentity(
        5, claim.plan_fingerprint, claim.review_fingerprint, claim.allowed_paths
    )
    with manager.acquire(binding, dirty_plan_identity=identity):
        pass
    pool = WorkerPool(1, threading.Event(), queue.Queue())
    try:
        if outcome == "scope":
            (original.cwd / "foreign.txt").write_text("outside scope\n")
        elif outcome == "no_changes":
            _git(original.cwd, "restore", "tracked.txt")

        def commit(*args: object, **kwargs: object) -> None:
            _git(original.cwd, "commit", "-m", "host commit test seam")

        with (
            patch.object(
                pool,
                "_verify_dirty_direct_plan",
                side_effect=(
                    [None, SourceWorkspaceError("changed plan")] if outcome == "plan_race" else None
                ),
            ) as plan_read,
            patch.object(
                pool,
                "_read_remote_branch_head",
                return_value=("f" * 40 if outcome == "remote" else head),
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool._controlled_git_signing_env",
                return_value=build_git_child_env(),
            ),
            patch(
                "hephaestus.automation.commit_runtime._commit_with_signature", side_effect=commit
            ) as commit_call,
            patch.object(pool, "_is_exact_recovery_commit", return_value=True),
            patch.object(
                pool,
                "_publish_commit_push",
                return_value=JobResult(
                    ok=outcome != "push_race", value={"pushed": outcome != "push_race"}
                ),
            ) as push,
        ):
            result = pool._run_git(
                GitJob(
                    repo="project",
                    op="publish_dirty_direct_continuation",
                    timeout_s=30,
                    expected_repository="example/project",
                    kwargs={
                        "repo_root": str(repo),
                        "issue_number": 12,
                        "source_workspace": binding.to_dict(),
                    },
                )
            )
        assert result.ok is (outcome == "success")
        receipt = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert receipt.dirty_claim is not None and receipt.dirty_claim.state == "consumed"
        assert "dirty-direct-continuation" in receipt.obligations
        if outcome in {"remote", "scope", "no_changes"}:
            commit_call.assert_not_called()
            push.assert_not_called()
            assert receipt.revision == head
        else:
            commit_call.assert_called_once()
            assert receipt.revision == _git(original.cwd, "rev-parse", "HEAD")
            assert receipt.revision != head
            assert plan_read.call_count == 2
            if outcome == "plan_race":
                push.assert_not_called()
            else:
                push.assert_called_once()
                assert push.call_args.args[0].kwargs["expected_remote_sha"] == head
    finally:
        pool._executor.shutdown()


@pytest.mark.parametrize(
    "outcome", ["start", "resume", "failure", "drift", "tool_mismatch", "model_mismatch"]
)
def test_native_dirty_codex_turn_keeps_one_use_claim(tmp_path: Path, outcome: str) -> None:
    """Native Codex consumes one valid claim and preserves failed writer content."""
    import queue
    import threading
    from unittest.mock import patch

    from hephaestus.agents.execution_policy import (
        AgentOperation,
        AgentRole,
        ExecutionRequest,
        SessionLifecycle,
    )
    from hephaestus.agents.runtime import AgentRunResult
    from hephaestus.agents.workspace import _DIRTY_PERMITS, _DirtyPermitRecord
    from hephaestus.automation.pipeline.jobs import AgentJob, DirtyDirectPlanInput
    from hephaestus.automation.pipeline.worker_pool import WorkerPool, _dirty_plan_input_identity

    repo, _, head = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="project")
    inputs = DirtyDirectPlanInput(
        5, "## Files to Modify\n- `tracked.txt`\n", 5, "state:plan-go", ("tracked.txt",)
    )
    identity = _dirty_plan_input_identity(inputs)
    claim = replace(
        _claim(),
        reservation_base_sha=head,
        plan_fingerprint=identity.plan_fingerprint,
        review_fingerprint=identity.review_fingerprint,
    )
    original = manager.prepare(12, SourceLane.IMPLEMENTATION, head, branch=claim.branch)
    pending = original.cwd / "tracked.txt"
    pending.write_text("pending\n")
    snapshot = _dirty_worktree_content_snapshot(original.cwd, timeout=10)
    claim = replace(
        claim,
        index_sha256=snapshot["index_sha256"],
        worktree_sha256=snapshot["worktree_sha256"],
        untracked_sha256=snapshot["untracked_sha256"],
    )
    binding = manager.claim_dirty_direct_continuation(
        12, claim=claim, expected_generation=original.generation
    )
    resume_id = (
        "native-dirty-session" if outcome in {"resume", "tool_mismatch", "model_mismatch"} else None
    )
    job = AgentJob(
        repo="project",
        issue=12,
        agent="codex",
        model="gpt-6-astra:low",
        prompt_builder=lambda: "prompt",
        cwd=original.cwd,
        timeout_s=30,
        workspace=binding,
        retryable=False,
        dirty_plan=inputs,
        resume_session_id=resume_id,
        resume_selection=("codex", "gpt-6-astra:low") if resume_id else None,
        execution_request=ExecutionRequest(
            AgentRole.IMPLEMENTER,
            AgentOperation.IMPLEMENT,
            SessionLifecycle.RESUME_REQUIRED if resume_id else SessionLifecycle.START_NEW,
        ),
    )
    if outcome in {"tool_mismatch", "model_mismatch"}:
        from hephaestus.automation.pipeline.coordinator_sessions import session_selection_error
        from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem

        previous = ("claude", job.model) if outcome == "tool_mismatch" else ("codex", "other:model")
        job = replace(job, resume_selection=previous)
        error = session_selection_error(
            WorkItem(repo="project", kind=ItemKind.ISSUE, issue=12), job
        )
        assert error == "session tool or model changed; start a new session"
        job = replace(job, session_selection_error=error)
    if outcome == "drift":
        pending.write_text("changed after claim\n")
    records: list[_DirtyPermitRecord] = []

    def native(*args: object, **kwargs: object) -> AgentRunResult:
        stored = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert stored.dirty_claim is not None and stored.dirty_claim.state == "consumed"
        active = _DIRTY_PERMITS.get()
        assert len(active) == 1 and active[0].active
        records.extend(active)
        if outcome == "failure":
            raise RuntimeError("provider failed")
        return AgentRunResult("done", "", "native-dirty-session")

    pool = WorkerPool(1, threading.Event(), queue.Queue())
    module = "hephaestus.automation.pipeline.worker_pool"
    try:
        with (
            patch(f"{module}.resolve_agent", return_value="codex"),
            patch(f"{module}.run_agent_session", side_effect=native) as start,
            patch(f"{module}.resume_agent_session", side_effect=native) as resume,
            patch.object(pool, "_run_codex_implementation") as adapter,
        ):
            result = pool._run_agent(job)
            assert result.ok is (outcome in {"start", "resume"}), result.error
            blocked = outcome in {"drift", "tool_mismatch", "model_mismatch"}
            expected_calls = 0 if blocked else 1
            assert start.call_count + resume.call_count == expected_calls
            assert resume.call_count == (1 if outcome == "resume" else 0)
            adapter.assert_not_called()
            replay = pool._run_agent(job)
            assert not replay.ok
            assert start.call_count + resume.call_count == expected_calls
        assert all(not record.active for record in records)
        stored = manager._require_receipt(12, SourceLane.IMPLEMENTATION)
        assert stored.dirty_claim is not None
        assert stored.dirty_claim.state == ("armed" if blocked else "consumed")
        assert pending.read_text() == (
            "changed after claim\n" if outcome == "drift" else "pending\n"
        )
    finally:
        pool._executor.shutdown()
