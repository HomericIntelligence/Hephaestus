"""Tests for the strict review loop in implementer.py."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.implementer import (
    MAX_REVIEW_ITERATIONS,
    MAX_REVIEW_ITERATIONS_HARD_CAP,
    IssueImplementer,
)
from hephaestus.automation.implementer_phase_runner import (
    _parse_dirty_reused_worktree_decision,
)
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


def _ambiguous() -> str:
    # No ``Verdict:`` line → parse_review_verdict returns AMBIGUOUS.
    return "Some prose with no verdict line.\n"


def _error() -> str:
    # Reviewer-infrastructure failure sentinel → parse_review_verdict returns ERROR.
    from hephaestus.automation.claude_invoke import INFRA_ERROR_REVIEW_TEXT

    return f"Reviewer invocation failed at iteration 0: boom\n\n{INFRA_ERROR_REVIEW_TEXT}"


def test_codex_implementer_advise_uses_codex_prompt_builder(
    implementer: IssueImplementer,
) -> None:
    """Codex implementer advise uses JSON selection via the Codex prompt."""
    implementer.options.agent = "codex"

    with (
        patch("hephaestus.automation._implement_phase.run_advise", return_value="findings") as run,
        patch("hephaestus.automation._implement_phase.run_agent_text") as direct_agent,
    ):
        direct_agent.return_value = subprocess.CompletedProcess(
            args=["codex", "exec"], returncode=0, stdout='{"skills": []}', stderr=""
        )
        result = implementer._run_advise(123, "Test Issue", "Issue body")
        invoke = run.call_args.kwargs["invoke"]
        assert invoke("prompt") == '{"skills": []}'

    assert result == "findings"
    assert run.call_args.kwargs["build_prompt"].__name__ == "get_codex_advise_prompt"
    assert direct_agent.call_args.kwargs["agent"] == "codex"
    assert direct_agent.call_args.kwargs["model"] == "gpt-5.4-mini"
    assert direct_agent.call_args.kwargs["sandbox"] == "read-only"


class TestRunImplReviewLoop:
    """Integration tests for the Stage 2 in-loop review + address cycle (#28).

    The loop now drives, per iteration:
      0. validation — re-open prior comments the diff did not address.
      1. ``_run_impl_review_step`` — a FRESH reviewer that posts inline PR
         threads and returns ``(verdict_text, posted_thread_ids)``.
      2. ``_run_address_review_step`` — resumes Session 2 to fix + resolve the
         posted threads (only when the verdict is not GO and threads exist).
    No separate ``review-prs`` / ``address-review`` OS process is spawned; both
    steps are in-process callables on the implementer.
    """

    @pytest.fixture(autouse=True)
    def _no_network(self) -> Iterator[None]:
        """Stub the loop's two GitHub-touching collaborators by default.

        The pre-address thread snapshot (``gh_pr_list_unresolved_threads``) and
        the per-iteration validation pass (``validate_prior_comments_addressed``)
        would otherwise hit the network. Tests that specifically exercise re-open
        behavior patch ``validate_prior_comments_addressed`` themselves.
        """
        with (
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation._review_phase.validate_prior_comments_addressed",
                return_value=([], True, set()),
            ),
            patch(
                "hephaestus.automation._review_phase.gh_issue_add_labels",
            ),
        ):
            yield

    def test_review_status_slot_shows_pr_number(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """During PR review work the status slot shows the PR number, not the issue.

        #5: when handling a PR, the PR number is the relevant identifier.
        """
        with (
            patch.object(implementer, "_run_impl_review_step", return_value=(_nogo("D"), ["t0"])),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch.object(implementer.status_tracker, "update_slot") as mock_slot,
            patch("hephaestus.automation._review_phase.gh_issue_add_labels"),
        ):
            implementer._run_impl_review_loop(
                issue_number=7,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=4242,
            )

        slot_texts = " | ".join(str(c.args[1]) for c in mock_slot.call_args_list if len(c.args) > 1)
        # "reviewing impl" status must reference the PR (#4242), not the issue (#7).
        assert "4242" in slot_texts
        assert "reviewing impl" in slot_texts

    def test_terminates_on_iter0_go(self, implementer: IssueImplementer, tmp_path: Path) -> None:
        with (
            patch.object(
                implementer, "_run_impl_review_step", return_value=(_go(), [])
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step") as mock_addr,
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
                pr_number=42,
            )

        assert iters == 1
        assert verdict == "GO"
        assert grade == "A"
        assert mock_rev.call_count == 1
        # Address step must NOT be called because iteration 0 already passed.
        mock_addr.assert_not_called()

    def test_forwards_advise_findings_to_review_step(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch.object(
            implementer, "_run_impl_review_step", return_value=(_go(), [])
        ) as mock_rev:
            implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=None,
                thread_id=None,
                pr_number=42,
                advise_findings="prior team finding",
            )

        assert mock_rev.call_args.kwargs["advise_findings"] == "prior team finding"

    def test_go_resolves_automation_threads_but_human_thread_blocks_go(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A GO cleans up stale automation comments AND is blocked by a human thread.

        The automation-owned threads are resolved, but the lone human review
        thread (alice) is left open and must BLOCK the GO — a GO cannot stand
        while a human review thread is unresolved (#1). Because automation cannot
        resolve a human thread, the loop breaks IMMEDIATELY with a distinct
        HUMAN_BLOCKED verdict (iters == 1) rather than spinning to exhaustion,
        and applies NO state:skip (the PR is left unlabeled, awaiting the human).
        """
        threads = [
            {"id": "T_self", "author": "mvillmow", "path": "a.py", "line": 1, "body": "old"},
            {
                "id": "T_bot",
                "author": "github-actions[bot]",
                "path": "b.py",
                "line": 2,
                "body": "old",
            },
            {
                "id": "T_nested_self",
                "author": "coderabbitai[bot]",
                "authors": ["coderabbitai[bot]", "mvillmow"],
                "comments": [
                    {"body": "old", "author": "coderabbitai[bot]"},
                    {"body": "automation reply", "author": "mvillmow"},
                ],
                "path": "d.py",
                "line": 4,
                "body": "old",
            },
            {
                "id": "T_other_bot",
                "author": "coderabbitai[bot]",
                "path": "e.py",
                "line": 5,
                "body": "bot",
            },
            {"id": "T_human", "author": "alice", "path": "c.py", "line": 3, "body": "human"},
        ]
        with (
            patch.object(implementer, "_run_impl_review_step", return_value=(_go(), [])),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch(
                "hephaestus.automation._review_phase.gh_current_login",
                return_value="mvillmow",
            ),
            patch("hephaestus.automation.github_api.gh_pr_resolve_thread") as mock_resolve,
            patch.object(implementer, "_run_address_review_step") as mock_addr2,
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            iters, verdict, _grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        # GO was blocked by an open human thread → distinct terminal verdict,
        # broke immediately (no spin to exhaustion), no address step, no skip.
        assert verdict == "HUMAN_BLOCKED"
        assert iters == 1
        mock_addr2.assert_not_called()
        mock_label.assert_not_called()  # no state:skip applied
        # The GO gate force-resolves NOTHING (#1152): a human thread is present,
        # so automation must never close it, and the gate no longer bulk-resolves
        # automation threads either (they'd be verified/addressed, but the human
        # block ends the loop first). The human thread is never resolved here.
        resolved_ids = {call.args[0] for call in mock_resolve.call_args_list}
        assert "T_human" not in resolved_ids

    def test_go_does_not_stand_while_automation_threads_unresolved(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A GO pass must NOT converge while automation threads remain unresolved (#1152).

        The OLD behavior force-resolved automation-owned threads on GO and
        accepted the verdict. That let a reviewer say GO while leaving real,
        unaddressed findings open — they were never fixed by the implementer nor
        verified by a re-review. The corrected rule: an unresolved automation
        thread downgrades GO to NOGO so the address step runs; the GO label is
        only earned by a clean pass with ZERO unresolved threads. Nothing is
        force-resolved here.
        """

        def _automation_thread(tid: str, author: str, path: str, line: int) -> dict[str, object]:
            return {"id": tid, "author": author, "path": path, "line": line, "body": "old"}

        with (
            # R0 emits GO but 2 automation threads are still unresolved; R1 sees a
            # clean board (address step fixed + resolved them) and GO stands. The
            # two open snapshots feed R0's GO gate and R1's pre-address snapshot.
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_go(), []), (_go(), [])],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                side_effect=[
                    [
                        _automation_thread("T_self", "mvillmow", "a.py", 1),
                        _automation_thread("T_bot", "github-actions[bot]", "b.py", 2),
                    ],
                    [
                        _automation_thread("T_self", "mvillmow", "a.py", 1),
                        _automation_thread("T_bot", "github-actions[bot]", "b.py", 2),
                    ],
                    [],
                ],
            ),
            patch(
                "hephaestus.automation._review_phase.gh_current_login",
                return_value="mvillmow",
            ),
            patch("hephaestus.automation.github_api.gh_pr_resolve_thread") as mock_resolve,
        ):
            iters, verdict, _grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        # GO did not converge at R0 (threads open) — address ran, R1 confirmed GO.
        assert verdict == "GO"
        assert iters == 2
        mock_addr.assert_called()  # the address step ran to fix the open threads
        # The GO gate itself force-resolved NOTHING; only the address step (which
        # we stubbed) resolves what it actually fixed.
        mock_resolve.assert_not_called()

    def test_go_does_not_converge_when_same_pass_posts_new_threads(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A reviewer that emits GO while posting NEW findings must not converge (#1152).

        This is the exact bug: ``Verdict: GO`` alongside freshly-posted inline
        threads. Those threads must be addressed and re-verified, never accepted
        as-is.
        """
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                # R0: GO but posts a brand-new thread t0; R1: clean GO.
                side_effect=[(_go(), ["t0"]), (_go(), [])],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                side_effect=[
                    [{"id": "t0", "author": "mvillmow", "path": "a.py", "line": 1, "body": "x"}],
                    [{"id": "t0", "author": "mvillmow", "path": "a.py", "line": 1, "body": "x"}],
                    [],
                ],
            ),
            patch(
                "hephaestus.automation._review_phase.gh_current_login",
                return_value="mvillmow",
            ),
        ):
            iters, verdict, _grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        assert verdict == "GO"
        assert iters == 2  # did NOT accept GO on R0; addressed then re-reviewed
        mock_addr.assert_called()

    def test_sustained_nogo_with_progress_extends_to_hard_cap(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A NOGO loop that keeps resolving findings extends to the hard cap.

        #1554: when every round resolves a fresh finding (``addressed`` and
        validator-clean) but the reviewer never issues a clean GO, the budget
        extends one iteration at a time up to ``MAX_REVIEW_ITERATIONS_HARD_CAP``,
        then stops. The hard cap bounds a reviewer that invents an endless stream
        of new findings so the loop can never spin forever.
        """
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                # One distinct NOGO thread per round, never going clean. Provide
                # enough side-effects to cover the hard cap.
                side_effect=[
                    (_nogo("D"), [f"t{i}"]) for i in range(MAX_REVIEW_ITERATIONS_HARD_CAP)
                ],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
        ):
            iters, verdict, _grade = implementer._run_impl_review_loop(
                issue_number=1,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        assert iters == MAX_REVIEW_ITERATIONS_HARD_CAP == 6
        assert verdict == "NOGO"
        assert mock_rev.call_count == MAX_REVIEW_ITERATIONS_HARD_CAP
        # Address runs on every iteration EXCEPT the final budgeted one.
        assert mock_addr.call_count == MAX_REVIEW_ITERATIONS_HARD_CAP - 1

    def test_address_resolving_nothing_breaks_loop_early(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """If the address step resolves no threads, the loop must stop, not spin."""
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), ["t0"]), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=False),
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
                pr_number=42,
            )

        assert iters == 1  # only the iter-0 review executed before the loop stopped
        assert verdict == "NOGO"
        assert mock_rev.call_count == 1

    def test_productive_loop_converges_past_base_budget_no_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A PR that fixes one real finding per round converges to GO (#1554).

        Reproduces the #1554 / PR #1570 strand: the reviewer surfaces a NEW
        distinct finding on each of R0/R1/R2 (one inline comment per pass), the
        implementer resolves each in turn, and only the FOURTH review is clean.
        Before the progress-aware budget the loop hit ``MAX_REVIEW_ITERATIONS``
        (3) one address-pass short — R2's finding was posted but never addressed,
        forcing a terminal NOGO and ``state:skip``. Now the budget extends while
        progress is made, so the loop reaches a clean GO and applies NO skip.
        """
        from hephaestus.automation.state_labels import STATE_SKIP

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[
                    (_nogo("D"), ["t0"]),  # R0: finding #1
                    (_nogo("C"), ["t1"]),  # R1: finding #2 (fresh)
                    (_nogo("B"), ["t2"]),  # R2: finding #3 (fresh) — was the strand point
                    (_go(), []),  # R3: clean — all addressed
                ],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            iters, verdict, grade = implementer._run_impl_review_loop(
                issue_number=1554,
                worktree_path=tmp_path,
                branch_name="1554-auto-impl",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=1570,
            )

        assert verdict == "GO"
        assert grade == "A"
        # Converged on the 4th review — one past the base budget of 3.
        assert iters == 4
        assert mock_rev.call_count == 4
        # The final-iteration address-skip off-by-one is fixed: R2's finding was
        # addressed (address ran on R0, R1, R2 — not on the clean R3).
        assert mock_addr.call_count == 3
        # A converged PR must NOT be tagged ``state:skip``.
        for call in mock_label.call_args_list:
            assert STATE_SKIP not in call.args[1], f"unexpected state:skip: {call}"

    def test_progress_then_clean_go_does_not_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """One productive round then a clean GO converges without skip (#1554).

        The base budget alone (3) would suffice here, but this guards the common
        case: progress is made, the next review is clean, and the loop terminates
        GO well within budget — no extension needed, no ``state:skip``.
        """
        from hephaestus.automation.state_labels import STATE_SKIP

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), ["t0"]), (_go(), [])],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
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
                pr_number=42,
            )

        assert verdict == "GO"
        assert iters == 2
        for call in mock_label.call_args_list:
            assert STATE_SKIP not in call.args[1]

    def test_exhaustion_without_go_applies_state_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Exhausting the review budget without GO applies ``state:skip``.

        #1083 Bug 2: a sustained NOGO run that never reaches GO must label the
        issue ``state:skip`` so the next loop skips it. #1554: a progress-making
        run extends to ``MAX_REVIEW_ITERATIONS_HARD_CAP`` first, but a hard-cap
        run that still never reaches GO is genuine exhaustion and is skipped.
        """
        from hephaestus.automation.state_labels import STATE_SKIP

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[
                    (_nogo("D"), [f"t{i}"]) for i in range(MAX_REVIEW_ITERATIONS_HARD_CAP)
                ],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=7,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        assert iters == MAX_REVIEW_ITERATIONS_HARD_CAP
        assert verdict == "NOGO"
        mock_label.assert_called_once_with(7, [STATE_SKIP])

    def test_infra_error_exhaustion_does_not_apply_state_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A run that exhausts on reviewer-infra ERROR must NOT apply ``state:skip``.

        Regression for #911 / PR #1069: a reviewer API 400 (advisor-tier
        mismatch) produced an ERROR every iteration; the PR was never reviewed,
        so skipping it strands a healthy PR. The issue must stay unlabeled for
        re-review.
        """
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[
                    (_error(), []),
                    (_error(), []),
                    (_error(), []),
                ],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            _, verdict, _ = implementer._run_impl_review_loop(
                issue_number=911,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=1069,
            )

        assert verdict == "ERROR"
        mock_label.assert_not_called()

    def test_infra_error_verdict_applies_neither_implementation_label(
        self, implementer: IssueImplementer
    ) -> None:
        """An ERROR verdict applies neither implementation-go nor -no-go.

        The PR was never reviewed, so it must be left unlabeled for the
        "no go/no-go label → re-review" path — not falsely marked no-go (which
        records a review that never happened) or go (which arms auto-merge on
        unreviewed code).
        """
        with (
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mock_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go") as mock_no_go,
        ):
            implementer.phase_runner._apply_impl_review_verdict(
                issue_number=911,
                pr_number=1069,
                last_verdict="ERROR",
                slot_id=None,
                thread_id=None,
            )

        mock_go.assert_not_called()
        mock_no_go.assert_not_called()

    def test_human_blocked_verdict_applies_neither_implementation_label(
        self, implementer: IssueImplementer
    ) -> None:
        """A HUMAN_BLOCKED verdict applies neither implementation-go nor -no-go.

        Review reached GO but an open human thread blocks it; the PR is left
        unlabeled for the human to resolve, not marked go (arming auto-merge on
        an unresolved PR) or no-go (recording a converged failure).
        """
        with (
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mock_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go") as mock_no_go,
        ):
            implementer.phase_runner._apply_impl_review_verdict(
                issue_number=911,
                pr_number=1069,
                last_verdict="HUMAN_BLOCKED",
                slot_id=None,
                thread_id=None,
            )

        mock_go.assert_not_called()
        mock_no_go.assert_not_called()

    def test_ambiguous_zero_threads_re_reviews_then_skips_on_exhaustion(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A zero-thread AMBIGUOUS pass re-reviews to exhaustion, THEN skips.

        Per the pr-review-loop skill (verified-ci): "zero threads != GO" — a
        single garbage/AMBIGUOUS review (e.g. a malformed verdict line) must NOT
        end the loop at R0. It re-reviews up to MAX_REVIEW_ITERATIONS and applies
        ``state:skip`` only on TRUE exhaustion.
        """
        from hephaestus.automation.state_labels import STATE_SKIP

        with (
            patch.object(
                implementer, "_run_impl_review_step", return_value=(_ambiguous(), [])
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step") as mock_addr,
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=8,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        assert verdict == "AMBIGUOUS"
        assert iters == MAX_REVIEW_ITERATIONS == 3  # re-reviewed to exhaustion
        assert mock_rev.call_count == 3
        # No threads ever posted → the address step never runs.
        mock_addr.assert_not_called()
        # Skip applied only on true exhaustion.
        mock_label.assert_called_once_with(8, [STATE_SKIP])

    def test_iter0_zero_thread_nogo_re_reviews_not_skip_at_r0(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A zero-thread NO-GO must NOT end the loop or skip at R0.

        It re-reviews (the next pass may surface threads or reach GO). Here the
        reviewer flips to GO on R1, so the loop converges WITHOUT skipping.
        """
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("C"), []), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step") as mock_addr,
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=8,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        assert verdict == "GO"
        assert iters == 2  # R0 NO-GO re-reviewed → R1 GO
        assert mock_rev.call_count == 2
        mock_addr.assert_not_called()  # no threads → nothing to address
        mock_label.assert_not_called()  # reached GO, no skip

    def test_go_does_not_apply_state_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A GO verdict must never apply ``state:skip``."""
        with (
            patch.object(implementer, "_run_impl_review_step", return_value=(_go(), [])),
            patch.object(implementer, "_run_address_review_step"),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
        ):
            implementer._run_impl_review_loop(
                issue_number=9,
                worktree_path=tmp_path,
                branch_name="b",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=42,
            )

        mock_label.assert_not_called()

    def test_dry_run_does_not_apply_state_skip(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Dry-run must not mutate labels even on a non-GO outcome."""
        implementer.options.dry_run = True
        try:
            with (
                patch.object(implementer, "_run_impl_review_step", return_value=(_nogo("C"), [])),
                patch.object(implementer, "_run_address_review_step"),
                patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
            ):
                implementer._run_impl_review_loop(
                    issue_number=10,
                    worktree_path=tmp_path,
                    branch_name="b",
                    issue_title="t",
                    issue_body="ib",
                    session_id="sess",
                    slot_id=0,
                    thread_id=None,
                    pr_number=42,
                )
            mock_label.assert_not_called()
        finally:
            implementer.options.dry_run = False

    def test_zero_thread_nogo_re_reviews_to_exhaustion(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A persistent zero-thread NO-GO re-reviews to exhaustion (does not stop at R0).

        "Zero threads != GO": a non-GO review with nothing actionable to address
        must NOT converge the loop — it re-reviews up to MAX_REVIEW_ITERATIONS so
        a transient bad review cannot strand a fixable PR.
        """
        with (
            patch.object(
                implementer, "_run_impl_review_step", return_value=(_nogo("C"), [])
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step") as mock_addr,
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
                pr_number=42,
            )

        assert iters == MAX_REVIEW_ITERATIONS == 3
        assert verdict == "NOGO"
        # No threads posted → nothing to address; loop re-reviews, never addresses.
        mock_addr.assert_not_called()
        assert mock_rev.call_count == 3

    def test_documented_by_design_recurrence_converges_to_go(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """#1329: once the validator accepts a documented by-design finding, GO stands.

        R0 the validator re-opens a finding (NO-GO → address). R1 the validator
        accepts it as documented-by-design (clean, nothing re-opened) and the
        reviewer says GO with a clean board → the loop converges to GO instead of
        spinning to exhaustion and labeling state:skip.
        """
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), []), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch("hephaestus.automation.github_api.gh_pr_resolve_thread"),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
            # R0: re-opens the finding (unclean). R1: documented by-design now →
            # clean, nothing re-opened, key dropped.
            patch.object(
                implementer.phase_runner,
                "_validate_prior_threads",
                side_effect=[(["RE1"], False, {"k1"}), ([], True, set())],
            ) as mock_validate,
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
                pr_number=42,
            )

        assert verdict == "GO"
        assert iters == 2
        assert mock_rev.call_count == 2
        # Addressed only after R0's re-open; R1 converged clean.
        assert mock_addr.call_count == 1
        assert mock_validate.call_count == 2
        mock_label.assert_not_called()  # converged to GO → no state:skip

    def test_no_session_id_still_addresses(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """session_id=None (existing-PR path) still addresses review threads.

        The address step resumes AGENT_IMPLEMENTER by its deterministic id, or
        starts a fresh implementer session bootstrapped with the task/diff — so
        a pre-existing PR with no initial session is fixed rather than
        dead-ending. The loop runs to GO on the second iteration here.
        """
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo(), ["t0"]), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
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
                pr_number=42,
            )

        assert iters == 2
        assert verdict == "GO"
        # Addressing ran despite no initial session_id, and was told to include
        # bootstrap context (no transcript guaranteed on this path).
        mock_addr.assert_called_once()
        assert mock_addr.call_args.kwargs["include_bootstrap_context"] is True
        assert mock_rev.call_count == 2

    def test_prior_review_passed_to_next_reviewer(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Each reviewer iteration receives the prior iteration's review."""
        review_r0 = _nogo("D")
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(review_r0, ["t0"]), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True),
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
                pr_number=42,
            )

        # R0: prior_review None
        assert mock_rev.call_args_list[0].kwargs["prior_review"] is None
        # R1: prior_review is R0's review
        assert mock_rev.call_args_list[1].kwargs["prior_review"] == review_r0
        # R0 has iteration=0; R1 has iteration=1
        assert mock_rev.call_args_list[0].kwargs["iteration"] == 0
        assert mock_rev.call_args_list[1].kwargs["iteration"] == 1

    def test_validator_reopen_forces_another_address_iteration(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A GO review cannot terminate while the validator re-opened a comment.

        R0 NOGO → address. R1 the validator re-opens a prior comment AND the
        reviewer now says GO — but the re-open must override GO, forcing the loop
        to address again rather than terminating with an unaddressed comment.
        By R2 the board is clean (no unresolved threads) so the GO stands (#1152:
        GO requires zero unresolved threads).

        ``_validate_prior_threads`` is patched directly to control the re-open
        signal per iteration, decoupling this assertion from the snapshot
        plumbing; the GO gate always sees a clean board so the ONLY thing keeping
        the loop alive past R1 is the validator re-open.
        """
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(
                implementer,
                "_run_impl_review_step",
                # R0 NOGO+threads, R1 GO (would terminate but for the re-open),
                # R2 GO (clean — terminates).
                side_effect=[(_nogo("D"), ["t0"]), (_go(), []), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=True) as mock_addr,
            # GO gate always sees a clean board (zero unresolved threads).
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch(
                "hephaestus.automation._review_phase.gh_current_login",
                return_value="mvillmow",
            ),
            patch("hephaestus.automation.github_api.gh_pr_resolve_thread"),
            # R0 clean, R1 re-opens (overrides GO), R2 clean. Each pass returns
            # the new (reopened_ids, is_clean, reopened_keys) tuple (#1329).
            patch.object(
                implementer.phase_runner,
                "_validate_prior_threads",
                side_effect=[([], True, set()), (["RE1"], False, {"k1"}), ([], True, {"k1"})],
            ) as mock_validate,
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
                pr_number=42,
            )

        # R1's GO was overridden by the re-open → addressed again → R2 GO ends it.
        assert iters == 3
        assert verdict == "GO"
        assert mock_rev.call_count == 3
        # Address ran after R0 and after the overridden R1 (not after R2's clean GO).
        assert mock_addr.call_count == 2
        # Validation ran every iteration (R0, R1, R2).
        assert mock_validate.call_count == 3

    def test_no_pr_falls_back_to_diff_only_reviewer(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """With no PR (dry-run / agent didn't open one), review falls back to diff-only.

        The diff-only fallback posts no inline threads and never addresses, so a
        sustained NOGO simply re-reviews until the iteration cap.
        """
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(
                implementer, "_run_impl_review", side_effect=[_nogo("D"), _nogo("C"), _nogo("B")]
            ) as mock_diff_rev,
            patch.object(implementer, "_run_address_review_step") as mock_addr,
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
                pr_number=None,
            )

        assert iters == MAX_REVIEW_ITERATIONS == 3
        assert verdict == "NOGO"
        assert mock_diff_rev.call_count == 3
        # No PR → in-loop address step is never invoked.
        mock_addr.assert_not_called()

    def test_no_commit_with_iterations_left_continues_not_break(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A no-commit address step does not end the loop while fixable work remains.

        #1554 regression: the address agent can resume a stale session and
        self-report success without committing. The #1083 guard returns
        ``addressed=False``; previously the loop hit ``if not addressed: break``
        and stopped at R0, wasting its remaining budget. Now, with unresolved
        threads still present and iterations left, the loop re-reviews and the
        next address attempt lands the fix → GO.
        """
        # Automation-owned thread (github-actions[bot]) so a GO with it still
        # open downgrades to NOGO (re-review) rather than HUMAN_BLOCKED.
        snapshot = [
            {
                "id": "t0",
                "path": "a.py",
                "line": 5,
                "body": "fix the guard",
                "author": "github-actions[bot]",
            }
        ]
        # The thread stays open until the address step commits (R1 returns True),
        # after which the list-threads helper reports it resolved so R1's GO
        # can finally stand. ``committed`` is flipped by the address side_effect.
        committed = {"done": False}

        def _addr(*_args: object, **_kwargs: object) -> bool:
            if committed["done"]:
                return True
            committed["done"] = True  # this turn lands the fix
            return False  # ...but reports no commit on the FIRST turn

        def _threads(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
            return [] if committed["done"] else snapshot

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), ["t0"]), (_go(), [])],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", side_effect=_addr) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                side_effect=_threads,
            ),
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=1554,
                worktree_path=tmp_path,
                branch_name="1554-auto-impl",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=1570,
            )

        # The loop did NOT break at R0's no-commit address step — it re-reviewed
        # (mock_rev twice) and converged on GO. (R1 was a clean GO, so the
        # address step ran only on R0; the key assertion is that the loop did
        # not stop after the first no-commit turn.)
        assert verdict == "GO"
        assert iters >= 2
        assert mock_rev.call_count >= 2
        assert mock_addr.call_count >= 1

    def test_no_commit_retry_injects_directive(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """After a no-commit turn, the NEXT address call carries the directive.

        #1554: the still-unresolved threads from the no-commit round are passed
        to the next address invocation via ``unaddressed_findings`` so the prompt
        names what to fix. The FIRST call (no prior no-commit turn) carries none.

        Both R0 and R1 stay NOGO with an open thread and the address step never
        commits, so two address turns occur: the second must carry the directive.
        """
        snapshot = [{"id": "t0", "path": "a.py", "line": 5, "body": "fix the guard"}]
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), ["t0"]), (_nogo("C"), ["t1"]), (_go(), [])],
            ),
            # Never commits → each round is a no-commit retry.
            patch.object(implementer, "_run_address_review_step", return_value=False) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=snapshot,
            ),
        ):
            implementer._run_impl_review_loop(
                issue_number=1554,
                worktree_path=tmp_path,
                branch_name="1554-auto-impl",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=1570,
            )

        assert mock_addr.call_count >= 2
        # First address turn: no prior no-commit, so empty/no directive.
        first = mock_addr.call_args_list[0]
        assert not first.kwargs.get("unaddressed_findings")
        # Second address turn: re-grounded with the still-unresolved snapshot.
        second = mock_addr.call_args_list[1]
        assert second.kwargs.get("unaddressed_findings") == snapshot

    def test_no_commit_thread_count_reduction_extends_budget_to_re_review(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A no-commit address turn still makes progress if unresolved threads decrease."""
        snapshots = [
            [{"id": "t0"}, {"id": "t1"}, {"id": "t2"}],
            [{"id": "t1"}, {"id": "t2"}],
            [{"id": "t1"}, {"id": "t2"}],
            [{"id": "t2"}],
            [{"id": "t2"}],
            [],
        ]

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[
                    (_nogo("D"), ["t0"]),
                    (_nogo("D"), ["t1"]),
                    (_nogo("D"), ["t2"]),
                    (_go(), []),
                ],
            ) as mock_rev,
            patch.object(implementer, "_run_address_review_step", return_value=False),
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch(
                "hephaestus.automation._review_phase.validate_prior_comments_addressed",
                return_value=([], True, set()),
            ) as validate,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                side_effect=snapshots,
            ),
        ):
            iters, verdict, _ = implementer._run_impl_review_loop(
                issue_number=1554,
                worktree_path=tmp_path,
                branch_name="1554-auto-impl",
                issue_title="t",
                issue_body="ib",
                session_id="sess",
                slot_id=0,
                thread_id=None,
                pr_number=1570,
            )

        assert verdict == "GO"
        assert iters == 4
        assert mock_rev.call_count == 4
        first_validated_threads = validate.call_args_list[0].kwargs["prior_threads"]
        assert [thread["id"] for thread in first_validated_threads] == ["t0", "t1", "t2"]

    def test_no_commit_then_commit_clears_directive(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A committed round clears the retry directive for the following turn.

        #1554: the directive must only follow an immediately-preceding no-commit
        turn. Sequence: R0 no-commit → R1 commits (clears) → R2 no-commit again →
        R3 should re-inject. We assert R2's address call (after the R1 commit)
        carries NO directive.
        """
        snapshot = [{"id": "t0", "path": "a.py", "line": 5, "body": "fix the guard"}]
        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                # Stay NOGO so the loop keeps addressing; budget extends while
                # progress is made, so provide enough reviews up to the hard cap.
                side_effect=[
                    (_nogo("D"), [f"t{i}"]) for i in range(MAX_REVIEW_ITERATIONS_HARD_CAP)
                ],
            ),
            # R0 no-commit, R1 commit, R2 no-commit, then commits.
            patch.object(
                implementer,
                "_run_address_review_step",
                side_effect=[False, True, False, True, True],
            ) as mock_addr,
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=snapshot,
            ),
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
                pr_number=42,
            )

        # Address call 0 (R0): no directive. Call 1 (R1, after R0 no-commit):
        # directive present. Call 2 (R2, after R1 commit): directive CLEARED.
        calls = mock_addr.call_args_list
        assert not calls[0].kwargs.get("unaddressed_findings")
        assert calls[1].kwargs.get("unaddressed_findings") == snapshot
        assert not calls[2].kwargs.get("unaddressed_findings")

    def test_normal_converging_run_reaches_go_unchanged(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Regression: a normal NOGO→GO run with committing fixes still converges.

        Guards against behavior drift from the no-commit-retry change: when the
        address step commits (``True``), the loop behaves exactly as before.
        """
        from hephaestus.automation.state_labels import STATE_SKIP

        with (
            patch.object(
                implementer,
                "_run_impl_review_step",
                side_effect=[(_nogo("D"), ["t0"]), (_go(), [])],
            ),
            patch.object(implementer, "_run_address_review_step", return_value=True),
            patch("hephaestus.automation._review_phase.gh_issue_add_labels") as mock_label,
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
                pr_number=42,
            )

        assert verdict == "GO"
        assert iters == 2
        for call in mock_label.call_args_list:
            assert STATE_SKIP not in call.args[1]


class TestRunImplReviewFailsSafe:
    """When the reviewer call itself fails, surface a distinct ERROR verdict.

    A reviewer-infrastructure failure (subprocess crash, API 400, timeout) must
    NOT be laundered into a real NOGO — that burns review iterations and
    triggers a spurious ``state:skip`` on a PR that was never reviewed
    (#911 / PR #1069). It emits an ERROR sentinel so the loop re-reviews
    instead.
    """

    def test_infra_failure_emits_error_verdict_not_nogo(
        self, implementer: IssueImplementer
    ) -> None:
        """Reviewer subprocess failure synthesizes an ERROR verdict, not NOGO."""
        from hephaestus.automation.claude_invoke import parse_review_verdict

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
        verdict = parse_review_verdict(out)
        assert verdict.verdict == "ERROR"
        assert verdict.is_error is True
        assert "Verdict: NOGO" not in out

    def test_direct_reviewer_uses_env_configured_review_timeout(
        self, implementer: IssueImplementer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct-agent implementation reviews use the centralized review timeout."""
        monkeypatch.setenv("HEPH_AGENT_REVIEW_TIMEOUT", "777")
        implementer.options.agent = "codex"

        with (
            patch("hephaestus.automation._review_phase.run_agent_text") as run_agent,
            patch(
                "hephaestus.automation._review_phase.direct_agent_model",
                return_value="review-model",
            ),
        ):
            run_agent.return_value = subprocess.CompletedProcess(
                ["codex"], 0, stdout="Grade: A\nVerdict: GO\n", stderr=""
            )

            out = implementer._run_impl_review(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                diff_text="d",
                files_changed="f",
                iteration=0,
                prior_review=None,
            )

        assert "Verdict: GO" in out
        assert run_agent.call_args.kwargs["timeout"] == 777

    def test_collect_diff_uses_env_configured_timeout(
        self, implementer: IssueImplementer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Diff collection uses the centralized diff timeout helper."""
        monkeypatch.setenv("HEPH_DIFF_COLLECT_TIMEOUT", "88")
        with patch("hephaestus.automation._review_phase.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["git", "diff"], 0, stdout="diff --git a/x b/x\n", stderr=""
            )

            assert implementer._collect_diff(tmp_path, "branch") == "diff --git a/x b/x\n"

        assert mock_run.call_args.kwargs["timeout"] == 88

    def test_collect_changed_files_uses_env_configured_git_timeout(
        self, implementer: IssueImplementer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Changed-file collection uses the centralized agent git timeout helper."""
        monkeypatch.setenv("HEPH_AGENT_GIT_TIMEOUT", "99")
        with patch("hephaestus.automation._review_phase.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                ["git", "diff"], 0, stdout="hephaestus/constants.py\n", stderr=""
            )

            assert implementer._collect_changed_files(tmp_path, "branch") == (
                "hephaestus/constants.py"
            )

        assert mock_run.call_args.kwargs["timeout"] == 99


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
        monkeypatch.setattr(
            "hephaestus.automation._review_phase.get_repo_slug", lambda _: "TestRepo"
        )
        monkeypatch.setattr(
            "hephaestus.automation._review_phase.current_trunk_githash", lambda _: "abc1234"
        )

    def test_resume_uses_session_id_and_prompt(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with patch("hephaestus.automation._review_phase.invoke_claude_with_session") as mock_invoke:
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
            "hephaestus.automation._review_phase.invoke_claude_with_session",
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

        with patch(
            "hephaestus.automation._review_phase.invoke_claude_with_session", side_effect=err
        ):
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

        with patch(
            "hephaestus.automation._review_phase.invoke_claude_with_session", side_effect=err
        ):
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
        data = json.loads(iter_file.read_text())
        assert data["iterations_run"] == 2
        assert prior_file.read_text() == "prior text"


class TestImplementationAutoMergeGate:
    """Implementation PR auto-merge must wait for implementation-review GO."""

    def _drive(
        self,
        implementer: IssueImplementer,
        tmp_path: Path,
        *,
        review_verdict: str,
        auto_merge: bool,
    ) -> None:
        implementer.options.auto_merge = auto_merge
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.ensure_pr_auto_merge_deferred"
            ) as mock_defer,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mock_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go") as mock_nogo,
            patch(
                "hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"
            ) as mock_arm,
            patch.object(
                implementer.worktree_manager, "create_worktree", return_value=worktree_path
            ),
            patch.object(implementer, "_has_plan", return_value=True),
            patch.object(implementer, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=True,
            ),
            patch.object(implementer, "_run_advise"),
            patch.object(implementer, "_run_claude_code", return_value="session-1"),
            patch.object(implementer, "_finalize_pr", return_value=456),
            patch.object(
                implementer, "_run_impl_review_loop", return_value=(1, review_verdict, "A")
            ),
            patch.object(implementer, "_run_post_pr_followup"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
        ):
            mock_issue.return_value.title = "title"
            mock_issue.return_value.body = "body"

            result = implementer._implement_issue(1)

        assert result.success is True
        mock_defer.assert_called_once_with(456)
        if review_verdict == "GO":
            mock_go.assert_called_once_with(456)
            mock_nogo.assert_not_called()
        else:
            mock_go.assert_not_called()
            mock_nogo.assert_called_once_with(456)
        if review_verdict == "GO" and auto_merge:
            mock_arm.assert_called_once_with(456)
        else:
            mock_arm.assert_not_called()

    def test_go_review_labels_pr_then_arms_auto_merge(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        self._drive(implementer, tmp_path, review_verdict="GO", auto_merge=True)

    def test_nogo_review_leaves_auto_merge_disabled(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        self._drive(implementer, tmp_path, review_verdict="NOGO", auto_merge=True)

    def test_go_review_respects_no_auto_merge_option(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        self._drive(implementer, tmp_path, review_verdict="GO", auto_merge=False)

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


class TestRunImplReviewStep:
    """Stage 2 (#28): _run_impl_review_step folds in the inline-PR reviewer."""

    def test_with_pr_posts_inline_threads_via_fresh_reviewer(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """With a PR, the step calls review_pr_inline with the per-iteration token."""
        with (
            patch.object(
                implementer.phase_runner,
                "_fetch_plan_and_review",
                return_value=("PLAN body", "## 🔍 Plan Review\nVerdict: GO"),
            ),
            patch.object(implementer, "_collect_diff", return_value="the-diff"),
            patch(
                "hephaestus.automation._review_phase.review_pr_inline",
                return_value=(_nogo("C"), ["thread-1", "thread-2"]),
            ) as mock_inline,
        ):
            text, thread_ids = implementer._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="ib",
                branch_name="b",
                worktree_path=tmp_path,
                pr_number=42,
                iteration=1,
                prior_review=None,
            )

        assert thread_ids == ["thread-1", "thread-2"]
        assert "NOGO" in text
        # The in-loop reviewer is invoked with the loop iteration so it derives
        # a FRESH per-iteration reviewer session (reviewer_agent(..., 1)).
        kwargs = mock_inline.call_args.kwargs
        assert kwargs["iteration"] == 1
        assert kwargs["pr_number"] == 42
        # TASK + PLAN + PLAN_REVIEW + diff are folded into the reviewer context.
        ctx = kwargs["context"]
        assert "the-diff" in ctx["pr_diff"]
        assert "PLAN body" in ctx["issue_body"]
        assert "Verdict: GO" in ctx["issue_body"]

    def test_passes_advise_findings_to_inline_review_context(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        with (
            patch.object(implementer.phase_runner, "_fetch_plan_and_review", return_value=("", "")),
            patch.object(implementer, "_collect_diff", return_value="the-diff"),
            patch(
                "hephaestus.automation._review_phase.review_pr_inline",
                return_value=(_go(), []),
            ) as mock_inline,
        ):
            implementer._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="ib",
                branch_name="b",
                worktree_path=tmp_path,
                pr_number=42,
                iteration=0,
                prior_review=None,
                advise_findings="prior team finding",
            )

        assert mock_inline.call_args.kwargs["context"]["advise_findings"] == "prior team finding"

    def test_reviewer_uses_per_iteration_session_token(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """review_pr_inline must derive the reviewer agent via reviewer_agent(..., i)."""
        from hephaestus.automation.session_naming import AGENT_PR_REVIEWER, reviewer_agent

        captured: dict[str, str] = {}

        def _fake_invoke(*, review_agent: str, **_: object) -> dict[str, object]:
            captured["review_agent"] = review_agent
            return {"comments": [], "summary": _go()}

        with (
            patch.object(implementer.phase_runner, "_fetch_plan_and_review", return_value=("", "")),
            patch.object(implementer, "_collect_diff", return_value="d"),
            patch(
                "hephaestus.automation.pr_reviewer.run_pr_review_analysis",
                side_effect=_fake_invoke,
            ),
            patch(
                "hephaestus.automation.pr_reviewer.gh_pr_review_post",
                return_value=[],
            ),
        ):
            implementer._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="ib",
                branch_name="b",
                worktree_path=tmp_path,
                pr_number=42,
                iteration=2,
                prior_review=None,
            )

        assert captured["review_agent"] == reviewer_agent(AGENT_PR_REVIEWER, 2)
        assert captured["review_agent"] == "pr-reviewer-r2"

    def test_no_pr_uses_diff_only_reviewer(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Without a PR, the step falls back to the diff-only reviewer (no posting)."""
        with (
            patch.object(implementer, "_collect_diff", return_value="diff"),
            patch.object(implementer, "_collect_changed_files", return_value="files"),
            patch.object(implementer, "_run_impl_review", return_value=_go()) as mock_diff_rev,
            patch("hephaestus.automation._review_phase.review_pr_inline") as mock_inline,
        ):
            text, thread_ids = implementer._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="ib",
                branch_name="b",
                worktree_path=tmp_path,
                pr_number=None,
                iteration=0,
                prior_review=None,
            )

        assert thread_ids == []
        assert "GO" in text
        mock_diff_rev.assert_called_once()
        mock_inline.assert_not_called()

    def test_inline_failure_emits_error_verdict(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """If the in-loop reviewer raises, the step returns an ERROR sentinel.

        Not a NOGO — a reviewer-infrastructure failure must be distinguishable
        from a genuine reviewer NOGO so the loop re-reviews instead of skipping
        (#911 / PR #1069).
        """
        from hephaestus.automation.claude_invoke import parse_review_verdict

        with (
            patch.object(implementer.phase_runner, "_fetch_plan_and_review", return_value=("", "")),
            patch.object(implementer, "_collect_diff", return_value="d"),
            patch(
                "hephaestus.automation._review_phase.review_pr_inline",
                side_effect=RuntimeError("reviewer down"),
            ),
        ):
            text, thread_ids = implementer._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="ib",
                branch_name="b",
                worktree_path=tmp_path,
                pr_number=42,
                iteration=0,
                prior_review=None,
            )

        assert thread_ids == []
        assert parse_review_verdict(text).verdict == "ERROR"
        assert "Verdict: NOGO" not in text


class TestRunAddressReviewStep:
    """Stage 2 (#28): _run_address_review_step folds in address-review in-loop."""

    def test_addresses_commits_and_pushes_but_does_not_resolve(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """The step fixes + commits + pushes but no longer resolves threads.

        #1083: resolution is now the validator's job (evidence-based).
        """
        threads = [
            {"id": "t1", "path": "a.py", "line": 1, "body": "fix a"},
            {"id": "t2", "path": "b.py", "line": 2, "body": "fix b"},
        ]
        with (
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch(
                "hephaestus.automation._review_phase.run_address_fix_session",
                return_value={"addressed": ["t1"], "replies": {"t1": "fixed a"}},
            ) as mock_fix,
            # A real commit was produced.
            patch.object(implementer.phase_runner, "_commit_if_changes", return_value=True),
            patch.object(implementer.phase_runner, "_push_branch") as mock_push,
        ):
            addressed = implementer._run_address_review_step(
                issue_number=1,
                pr_number=42,
                branch_name="b",
                worktree_path=tmp_path,
                iteration=0,
            )

        assert addressed is True
        assert mock_fix.call_args.kwargs["agent"] == implementer.options.agent
        mock_push.assert_called_once()
        # Resolution must NOT be imported/used here anymore.
        assert not hasattr(
            __import__(
                "hephaestus.automation.implementer_phase_runner",
                fromlist=["resolve_addressed_threads"],
            ),
            "resolve_addressed_threads",
        )

    def test_no_commit_means_not_addressed(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A self-reported fix with no commit must not count as addressed.

        #1083 Bug 1: a clean worktree means nothing changed, so the loop must
        not treat the session's self-report as progress.
        """
        threads = [{"id": "t1", "path": "a.py", "line": 1, "body": "fix a"}]
        with (
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch(
                "hephaestus.automation._review_phase.run_address_fix_session",
                return_value={"addressed": ["t1"], "replies": {"t1": "claimed"}},
            ),
            # Worktree was clean — no commit produced.
            patch.object(implementer.phase_runner, "_commit_if_changes", return_value=False),
            patch.object(implementer.phase_runner, "_push_branch") as mock_push,
        ):
            addressed = implementer._run_address_review_step(
                issue_number=1,
                pr_number=42,
                branch_name="b",
                worktree_path=tmp_path,
                iteration=0,
            )

        # No real change → the loop must not treat this as progress.
        assert addressed is False
        mock_push.assert_not_called()

    def test_no_unresolved_threads_returns_false(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """No unresolved threads → returns False without running a fix session."""
        with (
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=[],
            ),
            patch("hephaestus.automation._review_phase.run_address_fix_session") as mock_fix,
        ):
            addressed = implementer._run_address_review_step(
                issue_number=1,
                pr_number=42,
                branch_name="b",
                worktree_path=tmp_path,
                iteration=0,
            )

        assert addressed is False
        mock_fix.assert_not_called()

    def test_nothing_addressed_returns_false(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Fix session that addresses nothing → returns False (loop should stop)."""
        threads = [{"id": "t1", "path": "a.py", "line": 1, "body": "fix a"}]
        with (
            patch(
                "hephaestus.automation._review_phase.gh_pr_list_unresolved_threads",
                return_value=threads,
            ),
            patch(
                "hephaestus.automation._review_phase.run_address_fix_session",
                return_value={"addressed": [], "replies": {}},
            ),
            patch.object(implementer.phase_runner, "_commit_if_changes", return_value=False),
            patch.object(implementer.phase_runner, "_push_branch"),
        ):
            addressed = implementer._run_address_review_step(
                issue_number=1,
                pr_number=42,
                branch_name="b",
                worktree_path=tmp_path,
                iteration=0,
            )

        assert addressed is False


class TestFetchPlanAndReview:
    """_fetch_plan_and_review extracts the PLAN + PLAN_REVIEW comments."""

    def test_extracts_plan_and_review_bodies(
        self, implementer: IssueImplementer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.automation import review_state as review_state_mod

        comments = [
            {"body": "# Implementation Plan\n\nStep 1"},
            {"body": "## 🔍 Plan Review\n\nVerdict: GO"},
            {"body": "some unrelated comment"},
        ]
        monkeypatch.setattr(review_state_mod, "_fetch_issue_comments_graphql", lambda _n: comments)

        plan, review = implementer.phase_runner._fetch_plan_and_review(1)
        assert "Implementation Plan" in plan
        assert "Verdict: GO" in review

    def test_returns_empty_on_fetch_failure(
        self, implementer: IssueImplementer, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.automation import review_state as review_state_mod

        def _boom(_n: int) -> list:
            raise RuntimeError("graphql down")

        monkeypatch.setattr(review_state_mod, "_fetch_issue_comments_graphql", _boom)
        plan, review = implementer.phase_runner._fetch_plan_and_review(1)
        assert plan == ""
        assert review == ""


class TestCompactImplementerSession:
    """Test suite for _compact_implementer_session (#842)."""

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        """Create an IssueImplementer for testing."""
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

    def test_compact_runs_after_successful_learn(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """Verify /compact is called after /learn succeeds."""
        with patch("hephaestus.automation._implement_phase.compact_session") as mock_compact:
            mock_compact.return_value = True

            impl.phase_runner._compact_implementer_session(842, tmp_path)

            # Verify compact_session was called once
            assert mock_compact.call_count == 1
            call_kwargs = mock_compact.call_args[1]
            assert call_kwargs["issue"] == 842
            assert call_kwargs["cwd"] == tmp_path

    def test_compact_not_run_when_learn_failed(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """Verify /compact is not called when /learn returns False.

        Drives _run_post_pr_followup with enable_learn=True so the real guard
        at line 601 executes. The only reason compact is skipped is that
        _run_learn returns False (retro_success=False).
        """
        from hephaestus.automation.models import ImplementationState

        # Enable learn so the guard at line 601 is reached.
        impl.options.enable_learn = True
        impl.options.enable_follow_up = False

        state = ImplementationState(
            issue_number=42,
            session_id="test-session-abc",
            session_agent="claude",  # matches default agent="claude"
        )

        with patch.object(impl.phase_runner, "_run_learn", return_value=False):
            with patch.object(impl.phase_runner, "_compact_implementer_session") as mock_compact:
                impl.phase_runner._run_post_pr_followup(42, tmp_path, state, slot_id=None)

                # _run_learn returned False → retro_success=False → compact skipped
                mock_compact.assert_not_called()

    def test_compact_skipped_for_codex_implementer(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """Verify /compact is skipped for codex (no persisted session).

        Drives _run_post_pr_followup with enable_learn=True and agent="codex"
        so the real guard at line 601 executes. The only reason compact is
        skipped is that is_codex(agent) returns True.
        """
        from hephaestus.automation.models import ImplementationState

        # Enable learn so the guard at line 601 is reached.
        impl.options.enable_learn = True
        impl.options.enable_follow_up = False
        impl.options.agent = "codex"

        state = ImplementationState(
            issue_number=42,
            session_id="test-session-abc",
            session_agent="codex",  # matches agent="codex" so session is resumable
        )

        with patch.object(impl.phase_runner, "_run_learn", return_value=True):
            with patch.object(impl.phase_runner, "_compact_implementer_session") as mock_compact:
                impl.phase_runner._run_post_pr_followup(42, tmp_path, state, slot_id=None)

                # _run_learn returned True but agent is codex → compact skipped
                mock_compact.assert_not_called()


class TestReviewExistingPrShortCircuit:
    """``_review_existing_pr`` short-circuits on GO only; NO-GO re-enters the loop.

    A pre-existing PR labeled ``state:implementation-go`` is settled (auto-merge
    is drive-green's job) so re-review is skipped. A PR labeled
    ``state:implementation-no-go`` is NOT settled — it failed review and must be
    re-implemented + re-reviewed until it earns GO. Treating NO-GO as terminal
    (the prior ``has_go or has_no_go`` guard) left NO-GO PRs untouched every loop.
    """

    def _call(
        self,
        implementer: IssueImplementer,
        tmp_path: Path,
        *,
        has_go: bool,
        has_no_go: bool,
    ):
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(has_go, has_no_go),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="real-pr-branch",
            ),
            patch.object(
                implementer.worktree_manager, "create_worktree", return_value=worktree_path
            ) as mock_create_wt,
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as mock_sync,
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(implementer, "_run_advise"),
            patch.object(
                implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")
            ) as mock_loop,
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict") as mock_verdict,
        ):
            mock_issue.return_value.title = "title"
            mock_issue.return_value.body = "body"
            result = implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=555,
                branch_name="1-branch",
                state=state,
                slot_id=None,
                thread_id=None,
            )
        return result, mock_loop, mock_verdict, mock_sync, mock_create_wt

    def test_go_pr_short_circuits_without_review(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A GO-labeled PR returns success and does NOT re-run the review loop."""
        result, mock_loop, mock_verdict, mock_sync, _ = self._call(
            implementer, tmp_path, has_go=True, has_no_go=False
        )
        assert result.success is True
        assert result.already_has_pr is True
        assert result.pr_number == 555
        mock_loop.assert_not_called()
        mock_verdict.assert_not_called()
        mock_sync.assert_not_called()

    def test_no_go_pr_re_enters_review_loop(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A NO-GO-labeled PR re-runs implementation + review (the fix)."""
        result, mock_loop, mock_verdict, mock_sync, mock_create_wt = self._call(
            implementer, tmp_path, has_go=False, has_no_go=True
        )
        assert result.success is True
        mock_loop.assert_called_once()
        mock_verdict.assert_called_once()
        # Worktree is prepared + hard-reset on the PR's REAL head branch
        # (from get_pr_head_branch), NOT the assumed "1-branch" passed in.
        mock_sync.assert_called_once()
        assert mock_sync.call_args.args[1] == "real-pr-branch"
        assert mock_create_wt.call_args.args[1] == "real-pr-branch"
        assert result.branch_name == "real-pr-branch"

    def test_unlabeled_pr_runs_review_loop(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """An unlabeled existing PR runs the review loop (behavior preserved)."""
        result, mock_loop, mock_verdict, _, _ = self._call(
            implementer, tmp_path, has_go=False, has_no_go=False
        )
        assert result.success is True
        mock_loop.assert_called_once()
        mock_verdict.assert_called_once()

    def test_codex_existing_pr_forwards_advise_to_review_loop(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Codex existing-PR review has no transcript injection, so pass context."""
        implementer.options.agent = "codex"
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, False),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="b",
            ),
            patch.object(
                implementer.worktree_manager, "create_worktree", return_value=worktree_path
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(
                implementer, "_run_advise", return_value="prior team finding"
            ) as mock_advise,
            patch.object(
                implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")
            ) as mock_loop,
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict"),
            patch.object(implementer.phase_runner, "_run_post_pr_followup"),
        ):
            mock_issue.return_value.title = "title"
            mock_issue.return_value.body = "body"
            implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=555,
                branch_name="1-branch",
                state=state,
                slot_id=None,
                thread_id=None,
            )

        mock_advise.assert_called_once()
        assert mock_loop.call_args.kwargs["advise_findings"] == "prior team finding"

    @pytest.mark.parametrize("learn_completed", [False, True])
    def test_existing_pr_runs_post_review_learn_when_needed(
        self,
        implementer: IssueImplementer,
        tmp_path: Path,
        learn_completed: bool,
    ) -> None:
        """Existing-PR review mirrors fresh PR follow-up and does not repeat learn."""
        implementer.options.enable_learn = True
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        state = ImplementationState(
            issue_number=1,
            session_id="codex-session-1",
            session_agent="codex",
            learn_completed=learn_completed,
        )
        implementer.options.agent = "codex"
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, False),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="b",
            ),
            patch.object(
                implementer.worktree_manager, "create_worktree", return_value=worktree_path
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(implementer, "_run_advise"),
            patch.object(implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")),
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict"),
            patch.object(implementer.phase_runner, "_run_learn", return_value=True) as mock_learn,
        ):
            mock_issue.return_value.title = "title"
            mock_issue.return_value.body = "body"
            implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=555,
                branch_name="1-auto-impl",
                state=state,
                slot_id=None,
                thread_id=None,
            )

        if learn_completed:
            mock_learn.assert_not_called()
        else:
            mock_learn.assert_called_once()

    def test_no_go_falls_back_to_assumed_branch_when_lookup_fails(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """If get_pr_head_branch returns None, fall back to the passed-in name."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, True),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value=None,
            ),
            patch.object(
                implementer.worktree_manager, "create_worktree", return_value=worktree_path
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as mock_sync,
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(implementer, "_run_advise"),
            patch.object(implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")),
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict"),
        ):
            mock_issue.return_value.title = "title"
            mock_issue.return_value.body = "body"
            implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=555,
                branch_name="1-auto-impl",
                state=state,
                slot_id=None,
                thread_id=None,
            )
        assert mock_sync.call_args.args[1] == "1-auto-impl"


class TestResolveDirtyReusedWorktree:
    """A reused worktree that is dirty gets an agent commit-vs-stash decision."""

    def test_clean_worktree_skips_decision_agent(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """When the reused worktree is clean, no decision agent runs."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, True),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="b",
            ),
            patch.object(implementer.worktree_manager, "create_worktree", return_value=wt),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch.object(
                implementer.phase_runner, "_resolve_dirty_reused_worktree"
            ) as mock_resolve,
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(implementer, "_run_advise"),
            patch.object(implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")),
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict"),
        ):
            mock_issue.return_value.title = "t"
            mock_issue.return_value.body = "b"
            implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=5,
                branch_name="1-auto-impl",
                state=state,
                slot_id=None,
                thread_id=None,
            )
        mock_resolve.assert_not_called()

    def test_dirty_worktree_resolves_then_syncs_then_restores_and_pushes(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """Dirty resolution, hard reset, restore, and push happen in that order."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, True),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="b",
            ),
            patch.object(implementer.worktree_manager, "create_worktree", return_value=wt),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=False,
            ),
            patch.object(
                implementer.phase_runner, "_resolve_dirty_reused_worktree"
            ) as mock_resolve,
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as mock_sync,
            patch.object(
                implementer.phase_runner, "_restore_dirty_reused_worktree_commit_after_sync"
            ) as mock_restore,
            patch.object(implementer.phase_runner, "_push_branch") as mock_push,
            patch.object(implementer, "_save_state"),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info") as mock_issue,
            patch.object(implementer, "_run_advise"),
            patch.object(implementer, "_run_impl_review_loop", return_value=(1, "GO", "A")),
            patch.object(implementer.phase_runner, "_apply_impl_review_verdict"),
        ):
            mock_resolve.return_value = "abc123456789"
            parent = MagicMock()
            parent.attach_mock(mock_resolve, "resolve")
            parent.attach_mock(mock_sync, "sync")
            parent.attach_mock(mock_restore, "restore")
            parent.attach_mock(mock_push, "push")
            mock_issue.return_value.title = "t"
            mock_issue.return_value.body = "b"
            implementer.phase_runner._review_existing_pr(
                issue_number=1,
                existing_pr=5,
                branch_name="1-auto-impl",
                state=state,
                slot_id=None,
                thread_id=None,
            )
        assert [call[0] for call in parent.mock_calls[:4]] == [
            "resolve",
            "sync",
            "restore",
            "push",
        ]

    def test_dirty_resolver_failure_aborts_before_sync(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A failed stash/commit preservation refuses to run the destructive sync."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        state = ImplementationState(issue_number=1)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, True),
            ),
            patch.object(implementer.status_tracker, "update_slot"),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="b",
            ),
            patch.object(implementer.worktree_manager, "create_worktree", return_value=wt),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=False,
            ),
            patch.object(
                implementer.phase_runner,
                "_resolve_dirty_reused_worktree",
                side_effect=RuntimeError("Failed to stash dirty reused worktree"),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as mock_sync,
        ):
            with pytest.raises(RuntimeError, match="Failed to stash"):
                implementer.phase_runner._review_existing_pr(
                    issue_number=1,
                    existing_pr=5,
                    branch_name="1-auto-impl",
                    state=state,
                    slot_id=None,
                    thread_id=None,
                )
        mock_sync.assert_not_called()

    @pytest.mark.parametrize(
        ("output", "expected"),
        [
            ("Reasoning\nCOMMIT", "commit"),
            ("Reasoning mentions COMMIT\nSTASH", "stash"),
            ("I think COMMIT", "stash"),
            ("Reasoning\nDO NOT COMMIT", "stash"),
            ("", "stash"),
        ],
    )
    def test_dirty_worktree_decision_parser_requires_exact_final_line(
        self, output: str, expected: str
    ) -> None:
        """Only an exact final-line COMMIT chooses the commit path."""
        assert _parse_dirty_reused_worktree_decision(output) == expected

    def test_decision_agent_commit_verdict_commits(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A COMMIT verdict commits; STASH/default stashes (best-effort, no raise)."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        implementer.options.agent = "claude"
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.invoke_claude_with_session",
                return_value=("reasoning...\nCOMMIT", None),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_repo_slug",
                return_value="o/r",
            ),
            patch("hephaestus.automation.implementer_phase_runner.run") as mock_run,
        ):

            def fake_run(argv: list[str], **_: object) -> MagicMock:
                if argv[:3] == ["git", "rev-parse", "HEAD"]:
                    return MagicMock(stdout="abc123\n", returncode=0)
                return MagicMock(stdout="", returncode=0)

            mock_run.side_effect = fake_run
            implementer.phase_runner._resolve_dirty_reused_worktree(
                issue_number=1,
                worktree_path=wt,
                branch_name="708-auto-impl",
                thread_id=None,
            )
        argvs = [c[0][0] for c in mock_run.call_args_list]
        assert any(a[:3] == ["git", "commit", "-S"] for a in argvs), "COMMIT must be signed"
        assert any(a[:3] == ["git", "rev-parse", "HEAD"] for a in argvs)
        assert not any(a[:2] == ["git", "stash"] for a in argvs)

    def test_stash_failure_raises_instead_of_best_effort(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """A failed stash blocks the reset instead of silently continuing."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        implementer.options.agent = "claude"

        def fake_run(argv: list[str], **_: object) -> MagicMock:
            if argv[:3] == ["git", "stash", "push"]:
                raise subprocess.CalledProcessError(1, argv)
            return MagicMock(stdout="", returncode=0)

        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.invoke_claude_with_session",
                return_value=("reasoning...\nSTASH", None),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_repo_slug",
                return_value="o/r",
            ),
            patch("hephaestus.automation.implementer_phase_runner.run", side_effect=fake_run),
        ):
            with pytest.raises(RuntimeError, match="refusing to reset"):
                implementer.phase_runner._resolve_dirty_reused_worktree(
                    issue_number=1,
                    worktree_path=wt,
                    branch_name="708-auto-impl",
                    thread_id=None,
                )

    def test_restores_dirty_commit_after_sync_with_signed_dco_cherry_pick(
        self, implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        """The salvage commit is replayed with signed and DCO-signed cherry-pick."""
        wt = tmp_path / "worktree"
        wt.mkdir(exist_ok=True)
        with patch("hephaestus.automation.implementer_phase_runner.run") as mock_run:
            implementer.phase_runner._restore_dirty_reused_worktree_commit_after_sync(
                issue_number=1,
                worktree_path=wt,
                branch_name="708-auto-impl",
                commit_sha="abc123",
                thread_id=None,
            )
        mock_run.assert_called_once_with(
            ["git", "cherry-pick", "-S", "-s", "abc123"],
            cwd=wt,
            check=True,
        )
