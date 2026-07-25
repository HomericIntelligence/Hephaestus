"""Regression guards for human-owned PR review-thread resolution."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hephaestus.automation.models import CIDriverOptions
from hephaestus.automation.review_thread_resolver import ReviewThreadResolver


def test_claimed_drive_green_fix_requires_human_thread_resolution(tmp_path: Path) -> None:
    """A successful code push must not rearm a PR while its thread remains open."""
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

    with patch(
        "hephaestus.automation.review_thread_resolver.run_address_fix_session",
        return_value={"addressed": ["still-open"]},
    ):
        result = resolver.resolve_blocked_pr(1, 2, 0)

    assert result.success is False
    assert result.error == "human_review_thread_resolution_required"
    push.assert_called_once()


def test_thread_lookup_failure_requires_human_intervention(tmp_path: Path) -> None:
    """A failed thread read cannot be converted into a successful no-op."""
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
    assert result.error == "review_threads_unavailable"


def test_automation_sources_contain_no_review_thread_resolution_mutator() -> None:
    """No active automation source may resolve a GitHub review thread."""
    repo = Path(__file__).resolve().parents[3]
    targets = [
        path
        for path in sorted((repo / "hephaestus/automation").rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    forbidden = (
        "resolve" + "ReviewThread",
        "gh_pr_" + "resolve_thread",
        "mutation " + "ResolveThread",
    )

    offenders = {
        str(path.relative_to(repo)): token
        for path in targets
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    }

    assert offenders == {}
