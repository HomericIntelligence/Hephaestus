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


def test_automation_sources_contain_no_review_thread_resolution_mutator() -> None:
    """No active automation source may resolve a GitHub review thread."""
    repo = Path(__file__).resolve().parents[3]
    targets = [
        repo / "hephaestus/automation/pipeline_github.py",
        repo / "hephaestus/automation/address_review.py",
        repo / "hephaestus/automation/address_review_core.py",
        repo / "hephaestus/automation/review_validator.py",
        repo / "hephaestus/automation/review_thread_resolver.py",
        repo / "hephaestus/automation/ci_fix_flow.py",
        *sorted((repo / "hephaestus/automation/github_api").glob("*.py")),
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
