"""Tests for the strict review loop in implementer.py."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.implementer import MAX_REVIEW_ITERATIONS, IssueImplementer
from hephaestus.automation.models import ImplementationState, ImplementerOptions


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
    """Resume routes through invoke_claude_with_session.

    The Claude path derives the session UUID deterministically from
    ``(repo, issue, AGENT_IMPLEMENTER, githash)``, so the legacy
    ``session_id`` argument is consumed only for the error tag and the
    log message. ``invoke_claude_with_session(recreate_on_resume_failure=
    False)`` re-raises ``CalledProcessError`` so this method can decide
    whether to stop the review loop (expired) or just log (transient).
    """

    @pytest.fixture(autouse=True)
    def _repo_lookup(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("hephaestus.automation.implementer.get_repo_slug", lambda _: "TestRepo")
        monkeypatch.setattr(
            "hephaestus.automation.implementer.current_trunk_githash", lambda _: "abc1234"
        )

    def test_resume_uses_session_id_and_prompt(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch("hephaestus.automation.implementer.invoke_claude_with_session") as mock_invoke:
            mock_invoke.return_value = ("ok", "uuid")
            ok = implementer._resume_impl_with_feedback(
                session_id="abc",
                worktree_path=tmp_path,
                issue_number=1,
                review_text="Grade: D\nVerdict: NOGO",
                prev_iteration=0,
                verdict="NOGO",
            )

        assert ok is True
        kwargs = mock_invoke.call_args.kwargs
        assert kwargs["agent"] == "implementer"
        assert kwargs["issue"] == 1
        assert kwargs["recreate_on_resume_failure"] is False
        assert "Grade: D" in kwargs["prompt"] or "NOGO" in kwargs["prompt"]

    def test_resume_failure_returns_false(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch(
            "hephaestus.automation.implementer.invoke_claude_with_session",
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

    def test_session_expired_sets_state_error(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """CalledProcessError with session-expired stderr must tag state.error (#372)."""
        err = subprocess.CalledProcessError(1, ["claude"], stderr="session not found")
        state = ImplementationState(issue_number=1)

        with patch("hephaestus.automation.implementer.invoke_claude_with_session", side_effect=err):
            ok = implementer._resume_impl_with_feedback(
                session_id="ses123",
                worktree_path=tmp_path,
                issue_number=1,
                review_text="r",
                prev_iteration=0,
                verdict="NOGO",
                state=state,
            )

        assert ok is False
        assert state.error is not None
        assert state.error == "session_expired:ses123"

    def test_generic_process_error_does_not_set_session_expired(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Non-session CalledProcessError must NOT set the session_expired tag (#372)."""
        err = subprocess.CalledProcessError(1, ["claude"], stderr="network timeout")
        state = ImplementationState(issue_number=1)

        with patch("hephaestus.automation.implementer.invoke_claude_with_session", side_effect=err):
            ok = implementer._resume_impl_with_feedback(
                session_id="ses999",
                worktree_path=tmp_path,
                issue_number=1,
                review_text="r",
                prev_iteration=0,
                verdict="NOGO",
                state=state,
            )

        assert ok is False
        # Generic error: state.error must NOT carry the session_expired prefix
        assert state.error is None or not state.error.startswith("session_expired:")


class TestAmbiguousVerdictWarning:
    """A2-003: AMBIGUOUS or maxed-out loop must emit a warning."""

    def test_ambiguous_verdict_logs_warning(
        self, implementer: IssueImplementer, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """AMBIGUOUS verdict at end of loop must produce a logger.warning."""
        import logging

        ambiguous_text = "Not sure.\n\nGrade: C\nVerdict: AMBIGUOUS\n"
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(implementer, "_run_impl_review", return_value=ambiguous_text),
            patch.object(implementer, "_resume_impl_with_feedback", return_value=True),
        ):
            with caplog.at_level(logging.WARNING):
                _iters, verdict, _ = implementer._run_impl_review_loop(
                    issue_number=1,
                    worktree_path=tmp_path,
                    branch_name="b",
                    issue_title="t",
                    issue_body="ib",
                    session_id="sess",
                    slot_id=None,
                    thread_id=None,
                )

        assert verdict == "AMBIGUOUS"
        # At least one WARNING must mention the ambiguity
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("review loop ended" in m or "AMBIGUOUS" in m for m in warning_msgs), (
            f"Expected ambiguous-loop warning; got: {warning_msgs}"
        )

    def test_nogo_exhausted_loop_logs_warning(
        self, implementer: IssueImplementer, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Exhausting MAX_REVIEW_ITERATIONS with NOGO must also log a warning."""
        import logging

        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer,
                "_run_impl_review",
                side_effect=[_nogo("D"), _nogo("D"), _nogo("D")],
            ),
            patch.object(implementer, "_resume_impl_with_feedback", return_value=True),
        ):
            with caplog.at_level(logging.WARNING):
                iters, verdict, _ = implementer._run_impl_review_loop(
                    issue_number=1,
                    worktree_path=tmp_path,
                    branch_name="b",
                    issue_title="t",
                    issue_body="ib",
                    session_id="sess",
                    slot_id=None,
                    thread_id=None,
                )

        assert iters == MAX_REVIEW_ITERATIONS
        assert verdict == "NOGO"
        warning_msgs = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("review loop ended" in m for m in warning_msgs), (
            f"Expected exhausted-loop warning; got: {warning_msgs}"
        )


class TestReviewIterationStatePersistence:
    """A2-005: review iteration count and prior review must be persisted per iteration."""

    def test_save_review_iteration_state_called_each_iteration(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """_save_review_iteration_state must be called once per iteration."""
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer,
                "_run_impl_review",
                side_effect=[_nogo("D"), _nogo("C"), _go()],
            ),
            patch.object(implementer, "_resume_impl_with_feedback", return_value=True),
            patch.object(implementer, "_save_review_iteration_state") as mock_persist,
        ):
            iters, _, _ = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=None,
                thread_id=None,
            )

        # Loop ran 3 iterations (NOGO, NOGO, GO)
        assert iters == 3
        # Persist called once per iteration
        assert mock_persist.call_count == 3

    def test_persist_files_written_to_state_dir(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """_save_review_iteration_state must write iter JSON + prior-review text files."""
        implementer._save_review_iteration_state(
            issue_number=42, iterations_run=2, prior_review="prior text"
        )

        iter_file = implementer.state_dir / "review-iter-42.json"
        prior_file = implementer.state_dir / "review-prior-42.txt"
        assert iter_file.exists(), "review-iter-42.json not created"
        assert prior_file.exists(), "review-prior-42.txt not created"

        import json

        data = json.loads(iter_file.read_text())
        assert data["iterations_run"] == 2
        assert prior_file.read_text() == "prior text"

    def test_load_review_iteration_state_round_trips(self, implementer: IssueImplementer) -> None:
        """Round-trip: save then load must return the same values."""
        implementer._save_review_iteration_state(
            issue_number=7, iterations_run=1, prior_review="reviewer critique"
        )
        loaded_iters, loaded_prior = implementer._load_review_iteration_state(7)

        assert loaded_iters == 1
        assert loaded_prior == "reviewer critique"

    def test_load_returns_defaults_when_missing(self, implementer: IssueImplementer) -> None:
        """Loading state for a never-run issue returns (0, None)."""
        iters, prior = implementer._load_review_iteration_state(9999)
        assert iters == 0
        assert prior is None
