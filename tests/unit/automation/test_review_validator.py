"""Tests for hephaestus.automation.review_validator.

The validator runs a read-only sub-agent that compares prior review comments
against the current diff and re-opens (as NEW inline threads) the ones the diff
does not address.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from hephaestus.automation import review_validator


def _threads() -> list[dict[str, object]]:
    return [
        {"id": "T1", "path": "a.py", "line": 3, "body": "guard the null case"},
        {"id": "T2", "path": "b.py", "line": 7, "body": "rename for clarity"},
    ]


class TestValidatePriorCommentsAddressed:
    """Tests for validate_prior_comments_addressed."""

    def test_no_prior_threads_is_clean_noop(self, tmp_path: Path) -> None:
        with patch.object(review_validator, "gh_pr_review_post") as post:
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=[],
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()

    def test_dry_run_is_clean_noop(self, tmp_path: Path) -> None:
        with (
            patch.object(review_validator, "_run_validation_session") as run,
            patch.object(review_validator, "gh_pr_review_post") as post,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
                dry_run=True,
            )
        assert (reopened, is_clean) == ([], True)
        run.assert_not_called()
        post.assert_not_called()

    def test_all_addressed_posts_nothing(self, tmp_path: Path) -> None:
        with (
            patch.object(review_validator, "_run_validation_session", return_value=[]),
            patch.object(review_validator, "gh_pr_review_post") as post,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()

    def test_unaddressed_reopens_new_inline_thread(self, tmp_path: Path) -> None:
        unaddressed = [
            {
                "path": "a.py",
                "line": 3,
                "original_body": "guard the null case",
                "detail": "still dereferences x without a check",
            }
        ]
        with (
            patch.object(review_validator, "_run_validation_session", return_value=unaddressed),
            patch.object(
                review_validator, "gh_pr_review_post", return_value=["NEW_THREAD"]
            ) as post,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )

        assert reopened == ["NEW_THREAD"]
        assert is_clean is False
        post.assert_called_once()
        posted = post.call_args.kwargs["comments"]
        assert len(posted) == 1
        assert posted[0]["path"] == "a.py"
        assert posted[0]["line"] == 3
        assert posted[0]["side"] == "RIGHT"
        assert posted[0]["body"].startswith("Re-opening: prior review comment not addressed")
        # The original comment is quoted into the re-open body.
        assert "> guard the null case" in posted[0]["body"]

    def test_pr_level_unaddressed_without_path_is_skipped(self, tmp_path: Path) -> None:
        """An item with no path can't be an inline thread → skipped, stays clean."""
        unaddressed = [{"path": "", "line": None, "original_body": "x", "detail": "y"}]
        with (
            patch.object(review_validator, "_run_validation_session", return_value=unaddressed),
            patch.object(review_validator, "gh_pr_review_post") as post,
        ):
            reopened, is_clean = review_validator.validate_prior_comments_addressed(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="claude",
                iteration=1,
                state_dir=tmp_path,
            )
        assert reopened == []
        assert is_clean is True
        post.assert_not_called()
