"""Regression guards for retiring the legacy code-only thread resolver."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from hephaestus.automation.models import CIDriverOptions
from hephaestus.automation.review_thread_resolver import ReviewThreadResolver


def test_legacy_drive_green_resolver_refuses_to_mutate_threads(tmp_path: Path) -> None:
    """The former code-only path cannot bypass pipeline reviewer validation."""
    push = MagicMock(return_value=True)
    resolver = ReviewThreadResolver(
        options_provider=lambda: CIDriverOptions(dry_run=False),
        repo_root_provider=lambda: tmp_path,
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=MagicMock,
        get_worktree_path=lambda issue, pr: tmp_path,
        get_pr_branch=lambda pr: "fix-branch",
        sync_worktree_and_snapshot_sha=lambda issue, path, branch: "a" * 40,
        push_ci_fix=push,
        list_threads=lambda pr, dry_run: [{"id": "still-open"}],
    )

    result = resolver.resolve_blocked_pr(1, 2, 0)

    assert result.success is False
    assert result.error == "review_thread_resolver_retired_use_pipeline"
    push.assert_not_called()


def test_legacy_resolver_is_retired_before_a_thread_read(tmp_path: Path) -> None:
    """No legacy path may hand a thread to a human or make a code-only push."""
    resolver = ReviewThreadResolver(
        options_provider=lambda: CIDriverOptions(dry_run=False),
        repo_root_provider=lambda: tmp_path,
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=MagicMock,
        get_worktree_path=lambda issue, pr: tmp_path,
        get_pr_branch=lambda pr: "fix-branch",
        sync_worktree_and_snapshot_sha=lambda issue, path, branch: "a" * 40,
        push_ci_fix=MagicMock(return_value=True),
        list_threads=lambda pr, dry_run: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )

    result = resolver.resolve_blocked_pr(1, 2, 0)

    assert result.success is False
    assert result.error == "review_thread_resolver_retired_use_pipeline"


def test_only_pipeline_adapter_may_resolve_a_review_thread() -> None:
    """Stages cannot access a generic GitHub thread-resolution mutation."""
    repo = Path(__file__).resolve().parents[3]
    targets = [
        path
        for path in sorted((repo / "hephaestus/automation").rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    resolve_token = "resolve" + "ReviewThread"
    generic_tokens = ("gh_pr_" + "resolve_thread", "mutation " + "ResolveThread")
    resolve_offenders = {
        str(path.relative_to(repo)): token
        for path in targets
        for token in (resolve_token,)
        if token in path.read_text(encoding="utf-8")
    }
    generic_offenders = {
        str(path.relative_to(repo)): token
        for path in targets
        for token in generic_tokens
        if token in path.read_text(encoding="utf-8")
    }

    assert resolve_offenders == {"hephaestus/automation/pipeline_github.py": resolve_token}
    assert generic_offenders == {}
    adapter_source = (repo / "hephaestus/automation/pipeline_github.py").read_text(encoding="utf-8")
    assert adapter_source.index("addPullRequestReviewThreadReply") < adapter_source.index(
        resolve_token
    )
