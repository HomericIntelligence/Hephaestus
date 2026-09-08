"""Tests for terminal dirty direct-writer routing."""

from typing import Any

import pytest

from hephaestus.automation.pipeline.routing import Disposition
from hephaestus.automation.pipeline.stages import StageOutcome
from hephaestus.automation.pipeline.stages.implementation import ImplementationStage


@pytest.mark.parametrize(
    "state,payload",
    [
        ("TEST_WAIT", {"implement_error": True}),
        ("TESTFIX_WAIT", {}),
        ("COMMIT_PUSH_WAIT", {"tests_failed": True}),
    ],
)
def test_dirty_writer_failure_never_requests_another_turn(
    make_ctx: Any, make_work_item: Any, state: str, payload: dict[str, object]
) -> None:
    """A dirty continuation has one terminal attempt."""
    item = make_work_item(state=state)
    item.payload.update({"dirty_direct_active": True, **payload})
    outcome = ImplementationStage().step(item, make_ctx())
    assert isinstance(outcome, StageOutcome)
    assert outcome.disposition is Disposition.FINISH_FAIL


def test_dirty_probe_precedes_new_worktree_reservation(make_ctx: Any, make_work_item: Any) -> None:
    """Production worktrees receive an ownership probe before creation."""
    from pathlib import Path
    from types import SimpleNamespace

    from hephaestus.automation.pipeline.stages import JobRequest

    ctx = make_ctx(paths=SimpleNamespace(repo_root=Path("/tmp/repo"), source_workspaces=object()))
    item = make_work_item(state="WORKTREE_WAIT")
    result = ImplementationStage().step(item, ctx)
    assert isinstance(result, JobRequest)
    assert result.job.op == "claim_dirty_direct_continuation"
    assert result.on_done_state == "DIRTY_DIRECT_CLAIM_WAIT"


def test_dirty_marker_preserves_reservation_before_cleanup(
    make_ctx: Any, make_work_item: Any
) -> None:
    """No terminal cleanup releases a dirty continuation reservation."""
    from hephaestus.automation.pipeline.stages import Continue
    from hephaestus.automation.pipeline.stages.finished import FinishedStage

    item = make_work_item(
        state="CLEANUP",
        payload={
            "dirty_direct_preserve": True,
            "_direct_scope_reservation": {"branch": "writer", "base_sha": "a" * 40},
        },
    )
    item.worktree = "/tmp/repo/build/.worktrees/auto-1-impl"
    item.branch = "writer"
    preserved: list[tuple[str, int, str]] = []
    result = FinishedStage([], preserved, []).step(item, make_ctx())
    assert isinstance(result, Continue) and result.next_state == "DONE"
    assert preserved == [(item.repo, item.issue, item.worktree)]
    assert item.payload["_direct_scope_reservation"]["branch"] == "writer"


@pytest.mark.parametrize("ok,pushed", [(False, False), (True, False)])
def test_dirty_publication_completion_stops_before_pr_create(
    make_ctx: Any, make_work_item: Any, ok: bool, pushed: bool
) -> None:
    """The completion route cannot create a PR without a confirmed push."""
    from hephaestus.automation.pipeline.job_results import JobResult

    stage = ImplementationStage()
    ctx = make_ctx()
    item = make_work_item(state="COMMIT_PUSH_WAIT")
    item.payload.update({"dirty_direct_active": True, "dirty_direct_preserve": True})
    stage.on_job_done(item, JobResult(ok=ok, value={"pushed": pushed}), ctx)
    item.state = "PR_CREATE"
    result = stage.step(item, ctx)
    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert result.note.startswith("dirty_direct_publication_")
    assert item.pr is None
    assert item.payload["dirty_direct_preserve"] is True


def test_dirty_publication_completion_creates_strict_pr(
    make_ctx: Any, make_work_item: Any, tmp_path: Any
) -> None:
    """A confirmed push uses strict creation before it clears preservation."""
    from dataclasses import replace
    from unittest.mock import patch

    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from hephaestus.automation.pipeline.job_results import JobResult
    from tests.unit.agents.test_dirty_workspace import _claim

    stage = ImplementationStage()
    ctx = make_ctx()
    item = make_work_item(state="COMMIT_PUSH_WAIT", issue=12)
    claim = _claim()
    binding = replace(
        WorkspaceBinding.source(
            cwd=tmp_path / "writer",
            reusable_root=tmp_path,
            repository=item.repo,
            ownership_key="test",
            item_number=12,
            lane=SourceLane.IMPLEMENTATION,
            revision=claim.reservation_base_sha,
            generation=2,
            detached=False,
        ),
        schema_version=2,
        dirty_claim=claim,
    )
    item.branch = claim.branch
    item.payload.update(
        {
            "dirty_direct_active": True,
            "dirty_direct_preserve": True,
            "dirty_direct_binding": binding.to_dict(),
            "_direct_scope_reservation": {"branch": claim.branch},
        }
    )
    head = "b" * 40
    stage.on_job_done(item, JobResult(ok=True, value={"pushed": True, "head_sha": head}), ctx)
    item.state = "PR_CREATE"
    with (
        patch.object(ctx.github, "create_pr", return_value=17) as create,
        patch.object(
            ctx.github,
            "gh_pr_state",
            return_value={
                "state": "OPEN",
                "headRefOid": head,
                "baseRefName": "main",
            },
        ),
        patch.object(ctx.github, "get_pr_head_branch", return_value=claim.branch),
        patch.object(ctx.github, "pr_head_is_writable", return_value=True),
        patch(
            "hephaestus.automation.pipeline.stages.implementation.SourceWorkspaceManager"
        ) as manager,
    ):
        result = stage.step(item, ctx)
    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.ADVANCE
    assert create.call_args.kwargs["strict_absence"] is True
    manager.return_value.finish_dirty_direct_publication.assert_called_once_with(
        12,
        expected_head=head,
        pr_number=17,
    )
    assert "dirty_direct_preserve" not in item.payload
    assert "_direct_scope_reservation" not in item.payload


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("lazy", [False, True])
def test_failed_dirty_probe_records_only_its_deterministic_path(
    make_ctx: Any, make_work_item: Any, tmp_path: Any, foreign: bool, lazy: bool
) -> None:
    """A preserved probe failure retains its exact path for terminal reporting."""
    from types import SimpleNamespace

    from hephaestus.agents.workspace import SourceLane
    from hephaestus.automation.source_worktree import SourceWorkspaceManager
    from tests.unit.automation.test_source_worktree import _repository

    repo, _, _ = _repository(tmp_path)
    manager = SourceWorkspaceManager(repo, repository="repo")
    path = manager.path_for(12, SourceLane.IMPLEMENTATION)
    path.mkdir()
    ctx = make_ctx(
        paths=SimpleNamespace(
            repo_root=repo, source_workspaces=(lambda: manager) if lazy else manager
        )
    )
    item = make_work_item(state="DIRTY_DIRECT_CLAIM_WAIT", issue=12, repo="repo")
    item.payload.update(
        {
            "dirty_direct_preserve": True,
            "dirty_direct_claim_result": {
                "ok": False,
                "value": {"preserved_worktree": str(tmp_path if foreign else path)},
            },
        }
    )
    result = ImplementationStage().step(item, ctx)
    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert item.worktree == ("" if foreign else str(path))


@pytest.mark.parametrize("existing", ["issue", "branch"])
def test_stage_fake_strict_creation_cannot_hide_existing_pr(make_ctx: Any, existing: str) -> None:
    """The shared fake rejects issue links and branch PRs across bases."""
    github = make_ctx().github
    if existing == "issue":
        github.create_pr(12, "other-branch", "title", "Closes #12")
    else:
        github.gh_pr_create("writer", "title", "body", base="release")
    with pytest.raises(RuntimeError, match="existing PR"):
        github.create_pr(12, "writer", "title", "Closes #12", strict_absence=True)


@pytest.mark.parametrize(
    "global_agent,implementer_agent", [("claude", "codex"), ("codex", "claude")]
)
def test_dirty_turn_uses_the_implementer_tool(
    make_ctx: Any,
    make_work_item: Any,
    tmp_path: Any,
    global_agent: str,
    implementer_agent: str,
) -> None:
    """The dirty writer uses its role tool and keeps its one-use scope."""
    from dataclasses import asdict, replace

    from hephaestus.agents.workspace import SourceLane, WorkspaceBinding
    from hephaestus.automation.pipeline.jobs import DirtyDirectPlanInput
    from hephaestus.automation.pipeline.stages import JobRequest
    from tests.unit.agents.test_dirty_workspace import _claim

    claim = _claim()
    item = make_work_item(state="IMPLEMENT_WAIT", issue=12)
    binding = replace(
        WorkspaceBinding.source(
            cwd=tmp_path / "writer",
            reusable_root=tmp_path,
            repository=item.repo,
            ownership_key="test",
            item_number=12,
            lane=SourceLane.IMPLEMENTATION,
            revision=claim.reservation_base_sha,
            generation=2,
            detached=False,
        ),
        schema_version=2,
        dirty_claim=claim,
    )
    item.payload.update(
        dirty_direct_active=True,
        dirty_direct_binding=binding.to_dict(),
        dirty_direct_plan=asdict(
            DirtyDirectPlanInput(
                5, "## Files to Modify\n- `tracked.txt`", 5, "state:plan-go", ("tracked.txt",)
            )
        ),
    )
    ctx = make_ctx(
        config_overrides={
            "agent": global_agent,
            "implementer_agent": implementer_agent,
            "implementer_model": "gpt-6-astra:low",
        }
    )
    result = ImplementationStage().step(item, ctx)
    assert isinstance(result, JobRequest)
    assert result.job.agent == implementer_agent
    assert result.job.model == "gpt-6-astra:low"
    assert result.job.workspace == binding
    assert result.job.retryable is False
    assert result.job.dirty_plan.allowed_paths == ("tracked.txt",)
