"""Tests for the strict review loop in implementer.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.implementer import MAX_REVIEW_ITERATIONS, IssueImplementer
from hephaestus.automation.models import ImplementerOptions


@pytest.fixture
def implementer(tmp_path: Path) -> IssueImplementer:
    """IssueImplementer instance with a temp repo root for loop tests."""
    options = ImplementerOptions(
        epic_number=0,
        issues=[1],
        max_workers=1,
        skip_closed=True,
        auto_merge=False,
        dry_run=False,
        enable_learn=False,
        enable_follow_up=False,
        enable_ui=False,
    )
    with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
        impl = IssueImplementer(options)
    return impl


def _go() -> str:
    return "All good.\n\nGrade: A\nVerdict: GO\n"


def _nogo(grade: str = "D") -> str:
    return f"Findings.\n\nGrade: {grade}\nVerdict: NOGO\n"


class TestRunImplReviewLoop:
    """Integration tests for IssueImplementer._run_impl_review_loop."""

    def test_terminates_on_iter0_go(self, implementer: IssueImplementer, tmp_path: Path) -> None:
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(implementer, "_run_impl_review", return_value=_go()) as mock_rev,
            patch.object(implementer, "_resume_impl_with_feedback") as mock_resume,
        ):
            iters, verdict, grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
            )

        assert iters == 1
        assert verdict == "GO"
        assert grade == "A"
        assert mock_rev.call_count == 1
        # Resume must NOT be called because iteration 0 already passed
        mock_resume.assert_not_called()

    def test_runs_3_iterations_on_sustained_nogo(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer,
                "_run_impl_review",
                side_effect=[_nogo("D"), _nogo("C"), _nogo("B")],
            ) as mock_rev,
            patch.object(
                implementer, "_resume_impl_with_feedback", return_value=True
            ) as mock_resume,
        ):
            iters, verdict, grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
            )

        assert iters == MAX_REVIEW_ITERATIONS == 3
        assert verdict == "NOGO"
        assert grade == "B"  # last review's grade
        assert mock_rev.call_count == 3
        # Resume called for iterations 1 and 2 (not 0)
        assert mock_resume.call_count == 2

    def test_resume_failure_breaks_loop_early(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """If resume fails after R0 NOGO, the loop must stop, not silently rerun the review."""
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer, "_run_impl_review", side_effect=[_nogo("D"), _go()]
            ) as mock_rev,
            patch.object(implementer, "_resume_impl_with_feedback", return_value=False),
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
            )

        assert iters == 1  # only the iter-0 review executed
        assert verdict == "NOGO"
        assert mock_rev.call_count == 1

    def test_no_session_id_runs_only_iter0(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Without a session_id we cannot resume, so the loop runs once and stops."""
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(implementer, "_run_impl_review", return_value=_nogo()) as mock_rev,
            patch.object(implementer, "_resume_impl_with_feedback") as mock_resume,
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id=None,
                slot_id=0,
                thread_id=None,
            )

        assert iters == 1
        assert verdict == "NOGO"
        mock_resume.assert_not_called()
        assert mock_rev.call_count == 1

    def test_prior_review_passed_to_next_reviewer(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Each reviewer iteration receives the prior iteration's review."""
        review_r0 = _nogo("D")
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer, "_run_impl_review", side_effect=[review_r0, _go()]
            ) as mock_rev,
            patch.object(implementer, "_resume_impl_with_feedback", return_value=True),
        ):
            implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
            )

        # R0: prior_review None
        assert mock_rev.call_args_list[0].kwargs["prior_review"] is None
        # R1: prior_review is R0's review
        assert mock_rev.call_args_list[1].kwargs["prior_review"] == review_r0
        # R0 has iteration=0; R1 has iteration=1
        assert mock_rev.call_args_list[0].kwargs["iteration"] == 0
        assert mock_rev.call_args_list[1].kwargs["iteration"] == 1


class TestRunImplReviewFailsSafe:
    """When the reviewer call itself fails, treat as NoGo."""

    def test_synthetic_nogo_on_subprocess_failure(self, implementer: IssueImplementer) -> None:
        """Reviewer subprocess failure synthesizes a NOGO verdict."""
        with patch(
            "hephaestus.automation.implementer.subprocess.run",
            side_effect=RuntimeError("claude down"),
        ):
            out = implementer._run_impl_review(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                diff_text="d",
                files_changed="f",
                iteration=0,
                prior_review=None,
            )
        assert "Verdict: NOGO" in out
        assert "Grade: F" in out


class TestResumeImplWithFeedback:
    """Resume must use --resume <session_id> and pass the feedback prompt."""

    def test_resume_uses_session_id_and_prompt(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch("hephaestus.automation.implementer.run") as mock_run:
            ok = implementer._resume_impl_with_feedback(
                session_id="abc",
                worktree_path=tmp_path,
                issue_number=1,
                review_text="Grade: D\nVerdict: NOGO",
                prev_iteration=0,
                verdict="NOGO",
            )

        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "claude"
        assert "--resume" in cmd
        assert "abc" in cmd
        # The feedback prompt is one of the positional args; must reference iteration 0 critique
        joined = " ".join(cmd)
        assert "Grade: D" in joined or "NOGO" in joined

    def test_resume_failure_returns_false(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch(
            "hephaestus.automation.implementer.run",
            side_effect=RuntimeError("resume down"),
        ):
            ok = implementer._resume_impl_with_feedback(
                session_id="abc",
                worktree_path=tmp_path,
                issue_number=1,
                review_text="r",
                prev_iteration=0,
                verdict="NOGO",
            )
        assert ok is False
