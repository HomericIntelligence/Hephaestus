"""Regression tests for pre-review conflict resolution collaboration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from hephaestus.automation._review_conflict_resolver import (
    ConflictResolutionOperations,
    ConflictResolutionRequest,
    ReviewConflictResolver,
    build_conflict_resolution_operations,
)


def _request(*, slot_id: int | None = 4, thread_id: int | None = 9) -> ConflictResolutionRequest:
    """Build a request with stable issue, PR, and worktree context."""
    return ConflictResolutionRequest(
        issue_number=7,
        pr_number=12,
        worktree_path=Path("/worktree"),
        branch_name="7-auto-impl",
        slot_id=slot_id,
        thread_id=thread_id,
    )


@dataclass
class ResolverFakes:
    """Injected effects and their resolver for one deterministic test."""

    resolver: ReviewConflictResolver
    merge_state: mock.Mock
    base_branch: mock.Mock
    sync_worktree: mock.Mock
    rebase_worktree: mock.Mock
    resume: mock.Mock
    commit: mock.Mock
    push_rebased: mock.Mock
    push_agent: mock.Mock
    log: mock.Mock
    update_slot: mock.Mock


def _resolver(
    *,
    dry_run: bool = False,
    resume_result: bool = True,
    commit_result: bool = True,
) -> ResolverFakes:
    """Build a resolver using only injected, inspectable side effects."""
    merge_state = mock.Mock(return_value=("CLEAN", "MERGEABLE"))
    base_branch = mock.Mock(return_value="main")
    sync_worktree = mock.Mock()
    rebase_worktree = mock.Mock(return_value=False)
    resume = mock.Mock(return_value=resume_result)
    commit = mock.Mock(return_value=commit_result)
    push_rebased = mock.Mock()
    push_agent = mock.Mock()
    log = mock.Mock()
    update_slot = mock.Mock()
    resolver = ReviewConflictResolver(
        dry_run=lambda: dry_run,
        operations=ConflictResolutionOperations(
            merge_state=merge_state,
            base_branch=base_branch,
            sync_worktree=sync_worktree,
            rebase_worktree=rebase_worktree,
        ),
        resume_implementation=resume,
        commit_changes=commit,
        push_rebased_branch=push_rebased,
        push_agent_branch=push_agent,
        log=log,
        update_slot=update_slot,
    )
    return ResolverFakes(
        resolver=resolver,
        merge_state=merge_state,
        base_branch=base_branch,
        sync_worktree=sync_worktree,
        rebase_worktree=rebase_worktree,
        resume=resume,
        commit=commit,
        push_rebased=push_rebased,
        push_agent=push_agent,
        log=log,
        update_slot=update_slot,
    )


def test_dry_run_and_clean_or_unknown_states_avoid_git_and_agent_work() -> None:
    """Non-conflicting checks, including unknown GitHub state, are fail-open."""
    for dry_run, merge_state in (
        (True, ("DIRTY", "CONFLICTING")),
        (False, ("", "")),
        (False, ("CLEAN", "MERGEABLE")),
    ):
        fakes = _resolver(dry_run=dry_run)
        fakes.merge_state.return_value = merge_state

        assert fakes.resolver.resolve(_request()) is True

        fakes.sync_worktree.assert_not_called()
        fakes.resume.assert_not_called()
        fakes.commit.assert_not_called()
        fakes.push_rebased.assert_not_called()
        fakes.push_agent.assert_not_called()


def test_mechanical_rebase_uses_lease_push_then_requires_clean_readback() -> None:
    """A mechanical rebase uses its dedicated lease-push callback and rechecks the PR."""
    fakes = _resolver()
    fakes.merge_state.side_effect = [("DIRTY", "CONFLICTING"), ("CLEAN", "MERGEABLE")]
    fakes.rebase_worktree.return_value = True

    assert fakes.resolver.resolve(_request()) is True

    fakes.sync_worktree.assert_called_once_with(Path("/worktree"), "7-auto-impl")
    fakes.rebase_worktree.assert_called_once_with(Path("/worktree"), "main")
    fakes.push_rebased.assert_called_once_with("7-auto-impl", Path("/worktree"))
    fakes.push_agent.assert_not_called()
    fakes.resume.assert_not_called()
    fakes.commit.assert_not_called()


def test_agent_fallback_runs_only_after_mechanical_resolution_fails() -> None:
    """A conflicted mechanical rebase is explained before the agent fallback."""
    fakes = _resolver()
    fakes.merge_state.side_effect = [("DIRTY", "CONFLICTING"), ("CLEAN", "MERGEABLE")]

    assert fakes.resolver.resolve(_request()) is True

    fakes.resume.assert_called_once()
    fakes.commit.assert_called_once()
    fakes.push_rebased.assert_not_called()
    fakes.push_agent.assert_called_once_with("7-auto-impl", Path("/worktree"))
    assert any(
        "mechanical rebase hit conflicts; aborted; deferring to implementation agent"
        in str(call.args[1])
        for call in fakes.log.call_args_list
    )


@pytest.mark.parametrize(
    ("resume_result", "commit_result", "expected_pushes"),
    [(False, True, 0), (True, False, 0), (True, True, 1)],
)
def test_agent_commit_and_push_require_success_and_changes(
    resume_result: bool,
    commit_result: bool,
    expected_pushes: int,
) -> None:
    """The host pushes only a successful agent turn that produced changes."""
    fakes = _resolver(resume_result=resume_result, commit_result=commit_result)
    final_state = ("CLEAN", "MERGEABLE") if expected_pushes else ("DIRTY", "CONFLICTING")
    fakes.merge_state.side_effect = [("DIRTY", "CONFLICTING"), final_state]

    fakes.resolver.resolve(_request())

    fakes.resume.assert_called_once()
    assert fakes.commit.call_count == int(resume_result)
    assert fakes.push_rebased.call_count == 0
    assert fakes.push_agent.call_count == expected_pushes


@pytest.mark.parametrize("stdout", ["not-json", "null", '"main"'])
def test_base_branch_defaults_to_main_after_malformed_github_output(stdout: str) -> None:
    """Malformed GitHub JSON never prevents a conflict-resolution attempt."""
    github_call = mock.Mock(return_value=SimpleNamespace(stdout=stdout))
    operations = build_conflict_resolution_operations(
        Path("/repo"),
        github_call=github_call,
        repo_info=mock.Mock(return_value=("owner", "repo")),
        sync_worktree=mock.Mock(),
        rebase_worktree=mock.Mock(),
    )

    assert operations.base_branch(12) == "main"


def test_base_branch_defaults_to_main_after_github_error() -> None:
    """GitHub read failures use the same safe base-branch fallback."""
    operations = build_conflict_resolution_operations(
        Path("/repo"),
        github_call=mock.Mock(side_effect=RuntimeError("gh unavailable")),
        repo_info=mock.Mock(return_value=("owner", "repo")),
        sync_worktree=mock.Mock(),
        rebase_worktree=mock.Mock(),
    )

    assert operations.base_branch(12) == "main"


def test_github_read_failures_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Transient GitHub lookup failures remain diagnosable at their fallbacks."""
    operations = build_conflict_resolution_operations(
        Path("/repo"),
        github_call=mock.Mock(side_effect=RuntimeError("gh unavailable")),
        repo_info=mock.Mock(return_value=("owner", "repo")),
        sync_worktree=mock.Mock(),
        rebase_worktree=mock.Mock(),
    )

    with caplog.at_level(logging.DEBUG, logger="hephaestus.automation._review_conflict_resolver"):
        assert operations.merge_state(12) == ("", "")
        assert operations.base_branch(12) == "main"

    assert any(
        "Could not fetch Hephaestus#12 merge state" in message for message in caplog.messages
    )
    assert any(
        "Could not fetch Hephaestus#12 base branch" in message for message in caplog.messages
    )


@pytest.mark.parametrize("stdout", ["null", '"clean"'])
def test_malformed_merge_state_is_fail_open(stdout: str) -> None:
    """A non-object merge-state response is treated as unknown rather than conflicting."""
    operations = build_conflict_resolution_operations(
        Path("/repo"),
        github_call=mock.Mock(return_value=SimpleNamespace(stdout=stdout)),
        repo_info=mock.Mock(return_value=("owner", "repo")),
        sync_worktree=mock.Mock(),
        rebase_worktree=mock.Mock(),
    )

    assert operations.merge_state(12) == ("", "")


@pytest.mark.parametrize("final_state", [("DIRTY", "MERGEABLE"), ("CLEAN", "CONFLICTING")])
def test_confirmed_conflict_after_resolution_returns_false(final_state: tuple[str, str]) -> None:
    """A confirmed conflicting final readback is fail-closed."""
    fakes = _resolver()
    fakes.merge_state.side_effect = [("DIRTY", "CONFLICTING"), final_state, final_state]
    fakes.rebase_worktree.return_value = True

    assert fakes.resolver.resolve(_request()) is False

    assert any(
        "unresolved merge conflict" in str(call.args[1]) for call in fakes.log.call_args_list
    )


def test_status_and_log_callbacks_keep_issue_and_pr_context() -> None:
    """Operational callbacks retain the pre-existing issue/PR identifiers."""
    fakes = _resolver()
    fakes.merge_state.side_effect = [("DIRTY", "CONFLICTING"), ("CLEAN", "MERGEABLE")]
    fakes.rebase_worktree.return_value = True

    assert fakes.resolver.resolve(_request()) is True

    fakes.update_slot.assert_called_once_with(4, "PR #12: resolving merge conflict")
    assert "issue #7" in str(fakes.log.call_args.args[1])
    assert "PR #12" in str(fakes.log.call_args.args[1])
