"""Tests for the strict review loop in planner.py."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.models import PLAN_COMMENT_MARKER, PlannerOptions
from hephaestus.automation.planner import MAX_REVIEW_ITERATIONS, Planner
from hephaestus.automation.review_state import PLAN_REVIEW_PREFIX, is_plan_review_go


@pytest.fixture(autouse=True)
def _patch_loop_upsert() -> Any:
    """Stub the per-iteration comment upsert + label mutations so loop tests stay hermetic.

    ``PlanReviewLoop.run`` upserts the PLAN and REVIEW comments AND applies a
    ``state:*`` label via ``gh_issue_add_labels`` / ``gh_issue_remove_labels``
    (#704) on every iteration. Without these stubs the loop would issue real
    ``gh`` calls. Tests that need to inspect the upserts re-patch the upsert
    target locally; label-mutation tests re-patch the label targets the same way.
    """
    with (
        patch(
            "hephaestus.automation.planner_review_loop.gh_issue_upsert_comment",
            return_value=None,
        ) as mock_upsert,
        patch("hephaestus.automation.planner_review_loop.gh_issue_add_labels"),
        patch("hephaestus.automation.planner_review_loop.gh_issue_remove_labels"),
    ):
        yield mock_upsert


@pytest.fixture
def options() -> PlannerOptions:
    """PlannerOptions for the loop tests (advise disabled)."""
    return PlannerOptions(
        issues=[123],
        dry_run=False,
        force=False,
        parallel=1,
        skip_closed=True,
        enable_advise=False,  # skip advise entirely so we don't need to mock Mnemosyne
    )


@pytest.fixture
def planner(options: PlannerOptions) -> Planner:
    """Planner instance for loop tests."""
    return Planner(options)


def _go_review() -> str:
    return "All good.\n\nGrade: A\nVerdict: GO\n"


def _nogo_review(grade: str = "D") -> str:
    return f"Significant gaps.\n\nGrade: {grade}\nVerdict: NOGO\n"


def _error_review() -> str:
    # Reviewer-infrastructure failure sentinel → parse_review_verdict → ERROR.
    from hephaestus.automation.claude_invoke import INFRA_ERROR_REVIEW_TEXT

    return f"Reviewer invocation failed at iteration 0: boom\n\n{INFRA_ERROR_REVIEW_TEXT}"


class TestRunPlanReviewLoop:
    """Integration tests for _run_plan_review_loop end-to-end."""

    def test_terminates_immediately_on_iter0_go(self, planner: Planner) -> None:
        """A GO on R0 must skip iterations 1 and 2."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value="plan v0") as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value="learn0"),
            patch.object(planner, "_run_plan_review", return_value=_go_review()) as mock_review,
        ):
            plan, review, iters, verdict_is_go = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == 1
        assert plan == "plan v0"
        assert "Verdict: GO" in (review or "")
        assert mock_plan.call_count == 1
        assert mock_review.call_count == 1
        assert verdict_is_go is True

    def test_forwards_advise_findings_to_plan_reviewer(self, planner: Planner) -> None:
        """Plan review gets the same ProjectMnemosyne context as planning."""
        planner.options.enable_advise = True
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_run_advise", return_value="prior team learning"),
            patch.object(planner, "_generate_plan", return_value="plan v0"),
            patch.object(planner, "_capture_planner_learnings", return_value="learn0"),
            patch.object(planner, "_run_plan_review", return_value=_go_review()) as mock_review,
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        assert mock_review.call_args.kwargs["advise_findings"] == "prior team learning"

    def test_runs_all_3_iterations_on_sustained_nogo(self, planner: Planner) -> None:
        """When every review says NOGO, the loop runs exactly 3 iterations."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner,
                "_generate_plan",
                side_effect=["plan v0", "plan v1", "plan v2"],
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _nogo_review("C"), _nogo_review("B")],
            ) as mock_review,
        ):
            plan, review, iters, verdict_is_go = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == MAX_REVIEW_ITERATIONS == 3
        assert plan == "plan v2"
        assert "Verdict: NOGO" in (review or "")
        assert mock_plan.call_count == 3
        assert mock_review.call_count == 3
        assert verdict_is_go is False

    def test_terminates_on_go_at_iter1(self, planner: Planner) -> None:
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner, "_generate_plan", side_effect=["plan v0", "plan v1"]
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review(), _go_review()],
            ) as mock_review,
        ):
            plan, _review, iters, verdict_is_go = planner._run_plan_review_loop(123, slot_id=0)

        assert iters == 2
        assert plan == "plan v1"
        assert mock_plan.call_count == 2
        assert mock_review.call_count == 2
        assert verdict_is_go is True

    def test_infra_error_review_leaves_plan_labels_unchanged(self, planner: Planner) -> None:
        """A reviewer-infra ERROR must NOT apply state:plan-no-go.

        Mirrors the implementation-loop fix (#911): an infra failure is not a
        real verdict, so the plan stays unlabeled for re-review rather than
        being mislabeled no-go.
        """
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner,
                "_generate_plan",
                side_effect=["plan v0", "plan v1", "plan v2"],
            ),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_error_review(), _error_review(), _error_review()],
            ),
            patch.object(planner.review_loop, "_apply_state_label") as mock_label,
        ):
            _plan, _review, iters, verdict_is_go = planner._run_plan_review_loop(123, slot_id=0)

        # All three iterations were infra errors — never a real verdict.
        assert verdict_is_go is False
        assert iters == MAX_REVIEW_ITERATIONS == 3
        mock_label.assert_not_called()

    def test_prior_review_passed_to_next_plan(self, planner: Planner) -> None:
        """Iteration N+1 must receive iteration N's review as `prior_review`."""
        review_iter0 = _nogo_review("D")
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner, "_generate_plan", side_effect=["plan v0", "plan v1"]
            ) as mock_plan,
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[review_iter0, _go_review()],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        # First plan call: prior_review is None
        first_kwargs = mock_plan.call_args_list[0].kwargs
        assert first_kwargs.get("prior_review") is None

        # Second plan call: prior_review is the iter-0 NoGo review
        second_kwargs = mock_plan.call_args_list[1].kwargs
        assert second_kwargs.get("prior_review") == review_iter0


class TestLoopUpsertsPlanAndReview:
    """Stage 1: each loop iteration upserts ONE plan + ONE review comment.

    The issue must hold at most one ``# Implementation Plan`` and one
    ``## 🔍 Plan Review`` comment, both upserted in place rather than appended
    (the #455/#468/#484 self-review bug). These tests assert the loop calls
    ``gh_issue_upsert_comment`` with the canonical markers and normalises the
    bodies to start with those markers.
    """

    def test_upserts_plan_and_review_each_iteration(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """Three NOGO iterations → 3 plan upserts + 3 review upserts (6 total)."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", side_effect=["plan v0", "plan v1", "plan v2"]),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _nogo_review("C"), _nogo_review("B")],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        markers = [call.args[1] for call in _patch_loop_upsert.call_args_list]
        assert markers.count(PLAN_COMMENT_MARKER) == 3
        assert markers.count(PLAN_REVIEW_PREFIX) == 3

    def test_plan_body_gets_marker_prepended_when_missing(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A plan that lacks the marker must be upserted WITH the marker prefixed."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value="## Objective\nDo it"),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        plan_calls = [
            c for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_COMMENT_MARKER
        ]
        assert plan_calls, "expected a PLAN upsert"
        plan_body = plan_calls[0].args[2]
        assert plan_body.startswith(PLAN_COMMENT_MARKER)
        assert "## Objective" in plan_body

    def test_plan_body_passthrough_when_marker_present(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A plan that already starts with the marker is upserted unchanged (no double marker)."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value=f"{PLAN_COMMENT_MARKER}\n\nbody"),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        plan_calls = [
            c for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_COMMENT_MARKER
        ]
        plan_body = plan_calls[0].args[2]
        # Marker appears exactly once — not prepended a second time.
        assert plan_body.count(PLAN_COMMENT_MARKER) == 1

    def test_review_body_gets_prefix_prepended_when_missing(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A reviewer output lacking the prefix must be upserted WITH the prefix."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value="plan"),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        review_calls = [
            c for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_REVIEW_PREFIX
        ]
        assert review_calls, "expected a REVIEW upsert"
        assert review_calls[0].args[2].startswith(PLAN_REVIEW_PREFIX)

    def test_go_review_body_carries_go_verdict(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A GO loop verdict must upsert a REVIEW comment carrying Verdict: GO.

        The loop reviewer and the implementer's gate (is_plan_review_go) now
        speak the same GO/NOGO vocabulary, parsed by parse_review_verdict. The
        upserted review must therefore carry a GO verdict so the gate matches
        and the plan gets implemented.
        """
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value="plan"),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        review_body = next(
            c.args[2] for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_REVIEW_PREFIX
        )
        # is_plan_review_go parses the verdict via parse_review_verdict.
        assert is_plan_review_go(123, [{"body": review_body}]) is True
        assert "Verdict: GO" in review_body

    def test_nogo_review_body_carries_nogo_verdict(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A NOGO-exhausted loop must upsert a REVIEW that the gate reads as NOT go."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", side_effect=["v0", "v1", "v2"]),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _nogo_review("D"), _nogo_review("D")],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        review_body = [
            c.args[2] for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_REVIEW_PREFIX
        ][-1]
        assert "Verdict: NOGO" in review_body
        assert is_plan_review_go(123, [{"body": review_body}]) is False

    def test_replan_plan_body_has_changes_from_review_section(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """On a re-plan (iteration > 0), the upserted PLAN must carry a Changes-from-review section.

        Iteration 0 gets a NOGO, so iteration 1 re-plans with ``prior_review``
        set. The model output here omits the section, so the loop must append
        a defensive ``## Changes from review`` fallback.
        """
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", side_effect=["plan v0", "plan v1"]),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _go_review()],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        plan_calls = [
            c for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_COMMENT_MARKER
        ]
        # First plan (iteration 0, no prior review) must NOT have the section.
        assert "## Changes from review" not in plan_calls[0].args[2]
        # Second plan (iteration 1, re-plan) MUST have it.
        assert "## Changes from review" in plan_calls[1].args[2]

    def test_replan_does_not_duplicate_existing_changes_section(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """If the re-planned model output already has the section, no fallback is appended."""
        plan_with_section = "plan v1\n\n## Changes from review\n\nAddressed the prior NOGO."
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", side_effect=["plan v0", plan_with_section]),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(
                planner,
                "_run_plan_review",
                side_effect=[_nogo_review("D"), _go_review()],
            ),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        plan_calls = [
            c for c in _patch_loop_upsert.call_args_list if c.args[1] == PLAN_COMMENT_MARKER
        ]
        assert plan_calls[1].args[2].count("## Changes from review") == 1

    def test_upsert_failure_does_not_abort_loop(self, planner: Planner) -> None:
        """A failing upsert is non-fatal — the loop still completes and returns the plan."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_upsert_comment",
                side_effect=RuntimeError("github down"),
            ),
            patch.object(planner, "_generate_plan", return_value="plan v0"),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            plan, _review, iters, verdict_is_go = planner._run_plan_review_loop(123, slot_id=0)

        assert plan == "plan v0"
        assert iters == 1
        assert verdict_is_go is True


class TestPlanBodyContentGuard:
    """Stage 2 (#695): a changelog-shaped 'plan' must not be posted as a plan.

    Root cause of the #693 NOGO-exhaustion: the planner agent returned a
    meta-narrative/changelog instead of a plan body, which the loop then
    upserted verbatim and handed to the reviewer — who (correctly) NOGO'd a
    plan that contained no implementation content. The guard flags any plan
    body that lacks every expected section header with a visible operator
    warning, rather than silently posting the changelog as the plan.
    """

    def _plan_calls(self, mock_upsert: Any) -> list[Any]:
        return [c for c in mock_upsert.call_args_list if c.args[1] == PLAN_COMMENT_MARKER]

    def test_changelog_shaped_plan_gets_content_warning(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A 'plan' that is pure narrative (no section headers) is flagged, not posted clean."""
        changelog = (
            "Fixed and verified. The R1 reviewer was correct in substance: my prior "
            "`gh api PATCH` had accidentally overwritten the issue comment with only the "
            "chat-summary changelog, so the full plan was absent. The comment now contains "
            "the complete plan."
        )
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value=changelog),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_nogo_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        plan_calls = self._plan_calls(_patch_loop_upsert)
        assert plan_calls, "expected a PLAN upsert"
        body = plan_calls[0].args[2]
        # Still carries the marker (so the upsert keys correctly)...
        assert body.startswith(PLAN_COMMENT_MARKER)
        # ...but a visible warning marks it as not-a-real-plan so neither the
        # reviewer nor an operator mistakes the changelog for a plan.
        assert "PLAN-CONTENT-MISSING" in body

    def test_terse_but_valid_plan_passes_through_without_warning(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A short plan that DOES contain a recognised section header is not flagged."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(
                planner,
                "_generate_plan",
                return_value="## Objective\nFix X.\n\n## Verification\nRun the suite.",
            ),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        body = self._plan_calls(_patch_loop_upsert)[0].args[2]
        assert "PLAN-CONTENT-MISSING" not in body
        assert "## Objective" in body

    def test_leading_whitespace_marker_not_corrupted_by_banner(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A leading-whitespace marker must not be sliced mid-token by the banner insert.

        Regression (#700): when the plan arrives with leading whitespace before
        the marker, the passthrough branch kept the whitespace, so the banner
        insert's ``body[len(MARKER):]`` slice cut into the whitespace+marker and
        produced a corrupted/duplicated marker. The body must still begin with
        exactly one intact ``# Implementation Plan`` marker after flagging.
        """
        changelog = "\n\n# Implementation Plan\n\nFixed and verified. The comment now has the plan."
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value=changelog),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_nogo_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        body = self._plan_calls(_patch_loop_upsert)[0].args[2]
        assert body.startswith(PLAN_COMMENT_MARKER)
        assert body.count(PLAN_COMMENT_MARKER) == 1
        assert "PLAN-CONTENT-MISSING" in body

    def test_leading_whitespace_valid_plan_passes_clean(
        self, planner: Planner, _patch_loop_upsert: Any
    ) -> None:
        """A valid plan with leading whitespace + marker is unflagged and uncorrupted."""
        plan = "\n# Implementation Plan\n\n## Objective\nDo X."
        with (
            patch(
                "hephaestus.automation.planner_review_loop.gh_issue_json",
                return_value={"title": "T", "body": "B"},
            ),
            patch.object(planner, "_generate_plan", return_value=plan),
            patch.object(planner, "_capture_planner_learnings", return_value=""),
            patch.object(planner, "_run_plan_review", return_value=_go_review()),
        ):
            planner._run_plan_review_loop(123, slot_id=0)

        body = self._plan_calls(_patch_loop_upsert)[0].args[2]
        assert body.startswith(PLAN_COMMENT_MARKER)
        assert body.count(PLAN_COMMENT_MARKER) == 1
        assert "PLAN-CONTENT-MISSING" not in body
        assert "## Objective" in body


class TestCapturePlannerLearnings:
    """Learnings capture must fail safely (return '') without raising."""

    def test_returns_empty_on_call_claude_failure(self, planner: Planner) -> None:
        """Planner-learnings call failure is non-fatal — returns empty string."""
        with patch.object(planner, "_call_claude", side_effect=RuntimeError("claude down")):
            out = planner._capture_planner_learnings(123, "plan text")
        assert out == ""

    def test_returns_call_claude_output(self, planner: Planner) -> None:
        """Successful learnings call returns the output verbatim."""
        with patch.object(planner, "_call_claude", return_value="- learning A\n- learning B"):
            out = planner._capture_planner_learnings(123, "plan text")
        assert "learning A" in out

    def test_resumes_planner_session(self, planner: Planner) -> None:
        """Stage 1: capturing learnings RESUMES the planner session (AGENT_PLANNER).

        The learnings step must reuse the planner's own session so the model
        still "remembers" the plan it just wrote, rather than opening a
        separate AGENT_LEARNINGS session.
        """
        from hephaestus.automation.session_naming import AGENT_LEARNINGS, AGENT_PLANNER

        with patch.object(planner, "_call_claude", return_value="- learning A") as mock_call:
            planner._capture_planner_learnings(123, "plan text")

        agent = mock_call.call_args.kwargs["agent"]
        assert agent == AGENT_PLANNER
        assert agent != AGENT_LEARNINGS

    def test_uses_user_facing_learn_command(self, planner: Planner) -> None:
        """Planner learn capture must update ProjectMnemosyne through /learn."""
        with patch.object(planner, "_call_claude", return_value="- learning A") as mock_call:
            planner._capture_planner_learnings(123, "plan text")

        prompt = mock_call.call_args.args[0]
        assert prompt.startswith("/learn ")
        assert "EXECUTE the /learn skill-creation workflow" in prompt
        assert "ProjectMnemosyne" in prompt
        assert "/skills-registry-commands:learn" not in prompt
        assert "plan text" in prompt

    def test_writes_success_record(self, planner: Planner, tmp_path: Any) -> None:
        """A successful planner /learn attempt leaves durable evidence."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.get_repo_root",
                return_value=tmp_path,
            ),
            patch.object(planner, "_call_claude", return_value="- learning A\n"),
        ):
            out = planner._capture_planner_learnings(123, "plan text")

        state_dir = tmp_path / "build" / ".issue_implementer"
        record = json.loads((state_dir / "planner-learn-123.json").read_text())
        assert out == "- learning A\n"
        assert record["issue_number"] == 123
        assert record["learn_status"] == "succeeded"
        assert record["learn_attempted_at"]
        assert record["learn_succeeded_at"] == record["learn_attempted_at"]
        assert record["mnemosyne_update_status"] == "unverified"
        assert record["mnemosyne_update_urls"] == []
        assert record["mnemosyne_update_pr_numbers"] == []
        assert (state_dir / "planner-learn-123.log").read_text() == "- learning A\n"

    def test_writes_failure_record(self, planner: Planner, tmp_path: Any) -> None:
        """A failed planner /learn attempt is explicit but still non-fatal."""
        with (
            patch(
                "hephaestus.automation.planner_review_loop.get_repo_root",
                return_value=tmp_path,
            ),
            patch.object(planner, "_call_claude", side_effect=RuntimeError("claude down")),
        ):
            out = planner._capture_planner_learnings(123, "plan text")

        state_dir = tmp_path / "build" / ".issue_implementer"
        record = json.loads((state_dir / "planner-learn-123.json").read_text())
        assert out == ""
        assert record["issue_number"] == 123
        assert record["learn_status"] == "failed"
        assert record["learn_attempted_at"]
        assert record["learn_succeeded_at"] is None
        assert record["error"] == "claude down"
        assert record["mnemosyne_update_status"] == "failed"
        assert record["mnemosyne_update_urls"] == []
        assert record["mnemosyne_update_pr_numbers"] == []
        assert (state_dir / "planner-learn-123.log").read_text().startswith("FAILED:")


class TestRunPlanReview:
    """The reviewer must surface a distinct ERROR when the call itself errors."""

    def test_review_call_failure_returns_error_verdict_not_nogo(self, planner: Planner) -> None:
        """Reviewer-infra failure yields an ERROR verdict, not a fabricated NOGO.

        A genuine NOGO would stamp ``state:plan-no-go`` on a plan that was never
        reviewed; the ERROR sentinel keeps the plan unlabeled for re-review.
        """
        from hephaestus.automation.claude_invoke import parse_review_verdict

        with patch.object(planner, "_call_claude", side_effect=RuntimeError("review down")):
            out = planner._run_plan_review(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                plan_text="p",
                learnings="",
                iteration=0,
                prior_review=None,
            )
        assert parse_review_verdict(out).verdict == "ERROR"
        assert "Verdict: NOGO" not in out

    def test_uses_fresh_per_iteration_reviewer_session(self, planner: Planner) -> None:
        """Stage 1: the reviewer gets a FRESH session per iteration (``-r{iteration}``).

        The reviewer must never resume its own prior verdict — each iteration
        is an unbiased fresh session keyed on ``reviewer_agent(...)``.
        """
        from hephaestus.automation.session_naming import AGENT_PLAN_REVIEWER, reviewer_agent

        captured: list[str] = []

        def _capture(*_args: Any, **kwargs: Any) -> str:
            captured.append(kwargs["agent"])
            return _go_review()

        with patch.object(planner, "_call_claude", side_effect=_capture):
            for i in range(3):
                planner._run_plan_review(
                    issue_number=1,
                    issue_title="t",
                    issue_body="b",
                    plan_text="p",
                    learnings="",
                    iteration=i,
                    prior_review=None,
                )

        assert captured == [
            reviewer_agent(AGENT_PLAN_REVIEWER, 0),
            reviewer_agent(AGENT_PLAN_REVIEWER, 1),
            reviewer_agent(AGENT_PLAN_REVIEWER, 2),
        ]
        # Each iteration is a distinct session token.
        assert len(set(captured)) == 3


class TestPostPlanWithReview:
    """The final plan comment must be UPSERTED and include the final-review block.

    ``_post_plan`` now upserts the single ``# Implementation Plan`` comment via
    ``gh_issue_upsert_comment(issue_number, PLAN_COMMENT_MARKER, body)`` instead
    of appending via ``gh_issue_comment``, so the issue never accumulates
    duplicate plan comments (#455/#468/#484). The body is the third positional
    argument (marker is the second).
    """

    def test_post_plan_upserts_with_plan_marker(self, planner: Planner) -> None:
        """The upsert must key off the canonical PLAN_COMMENT_MARKER."""
        from hephaestus.automation.models import PLAN_COMMENT_MARKER

        with patch("hephaestus.automation.planner.gh_issue_upsert_comment") as mock_cmt:
            planner._post_plan(123, "plan body", final_review="Grade: B\nVerdict: GO")
        assert mock_cmt.call_args[0][0] == 123
        assert mock_cmt.call_args[0][1] == PLAN_COMMENT_MARKER
        body = mock_cmt.call_args[0][2]
        assert body.startswith(PLAN_COMMENT_MARKER)

    def test_post_plan_includes_review_when_present(self, planner: Planner) -> None:
        with patch("hephaestus.automation.planner.gh_issue_upsert_comment") as mock_cmt:
            planner._post_plan(123, "plan body", final_review="Grade: B\nVerdict: GO")
        body = mock_cmt.call_args[0][2]
        assert "plan body" in body
        assert "Grade: B" in body
        assert "Verdict: GO" in body

    def test_post_plan_omits_review_block_when_none(self, planner: Planner) -> None:
        with patch("hephaestus.automation.planner.gh_issue_upsert_comment") as mock_cmt:
            planner._post_plan(123, "plan body", final_review=None)
        body = mock_cmt.call_args[0][2]
        assert "plan body" in body
        assert "Final review verdict" not in body

    def test_post_plan_includes_nogo_banner_when_verdict_is_false(self, planner: Planner) -> None:
        """When verdict_is_go=False a NOGO-EXHAUSTED banner must appear in the comment (#369)."""
        with patch("hephaestus.automation.planner.gh_issue_upsert_comment") as mock_cmt:
            planner._post_plan(
                123,
                "plan body",
                final_review="Grade: D\nVerdict: NOGO",
                verdict_is_go=False,
            )
        body = mock_cmt.call_args[0][2]
        assert "NOGO-EXHAUSTED" in body
        assert "plan body" in body

    def test_post_plan_omits_nogo_banner_on_go(self, planner: Planner) -> None:
        """A GO plan must not include the NOGO banner."""
        with patch("hephaestus.automation.planner.gh_issue_upsert_comment") as mock_cmt:
            planner._post_plan(
                123,
                "plan body",
                final_review="Grade: A\nVerdict: GO",
                verdict_is_go=True,
            )
        body = mock_cmt.call_args[0][2]
        assert "NOGO-EXHAUSTED" not in body
        assert "plan body" in body


class TestPlanIssueNOGOExhausted:
    """_plan_issue must return PlanResult.success=False when the loop is NOGO-exhausted (#369)."""

    def test_nogo_exhausted_plan_result_success_false(self, planner: Planner) -> None:
        """All-NOGO loop must produce PlanResult(success=False) not PlanResult(success=True)."""
        with (
            patch.object(planner, "_has_existing_plan", return_value=False),
            patch.object(
                planner,
                "_run_plan_review_loop",
                return_value=("plan text", "Grade: D\nVerdict: NOGO\n", 3, False),
            ),
            patch.object(planner, "_post_plan"),
        ):
            result = planner._plan_issue(123)

        assert result.success is False
        assert result.error is not None
        assert "NOGO" in result.error

    def test_go_plan_result_success_true(self, planner: Planner) -> None:
        """A GO loop must still produce PlanResult(success=True)."""
        with (
            patch.object(planner, "_has_existing_plan", return_value=False),
            patch.object(
                planner,
                "_run_plan_review_loop",
                return_value=("plan text", "Grade: A\nVerdict: GO\n", 1, True),
            ),
            patch.object(planner, "_post_plan"),
        ):
            result = planner._plan_issue(123)

        assert result.success is True

    def test_existing_plan_short_circuits_in_worker(self, planner: Planner) -> None:
        """In-worker skip path (#548).

        When a plan already exists, _plan_issue must return early with
        plan_already_exists=True and NOT invoke the review loop or post a
        plan. This is the parallel replacement for the old serial pre-pass
        in _filter_issues.
        """
        with (
            patch.object(planner, "_has_existing_plan", return_value=True),
            patch.object(planner, "_run_plan_review_loop") as mock_loop,
            patch.object(planner, "_post_plan") as mock_post,
        ):
            result = planner._plan_issue(123)

        assert result.success is True
        assert result.plan_already_exists is True
        mock_loop.assert_not_called()
        mock_post.assert_not_called()

    def test_force_bypasses_existing_plan_check(self, planner: Planner) -> None:
        """With force=True, the skip-check must be bypassed (#548).

        _plan_issue must NOT call _has_existing_plan and must run the review
        loop unconditionally. Keeps --force semantics intact after moving the
        skip-check into the worker.
        """
        planner.options.force = True
        with (
            patch.object(planner, "_has_existing_plan") as mock_check,
            patch.object(
                planner,
                "_run_plan_review_loop",
                return_value=("plan", _go_review(), 1, True),
            ) as mock_loop,
            patch.object(planner, "_post_plan"),
        ):
            result = planner._plan_issue(123)

        mock_check.assert_not_called()
        mock_loop.assert_called_once()
        assert result.success is True
        assert result.plan_already_exists is False


class TestFilterIssues:
    """Regression guards for #548.

    _filter_issues must NOT do per-issue plan lookups (those moved into the
    worker). If these tests fail, the per-issue gh round-trip stall at phase
    1 startup has been re-introduced.
    """

    def test_filter_does_not_call_has_existing_plan(self, planner: Planner) -> None:
        """The serial _gh_call pre-pass is gone.

        _filter_issues must not invoke _has_existing_plan, even once.
        """
        with (
            patch.object(planner, "_has_existing_plan") as mock_check,
            patch(
                "hephaestus.automation.planner_state.prefetch_issue_states",
                return_value={},
            ),
        ):
            result = planner._filter_issues()

        mock_check.assert_not_called()
        assert result == [123]

    def test_filter_still_skips_closed_issues(self, planner: Planner) -> None:
        """Closed-issue filtering (cheap, batched GraphQL) stays in the pre-pass."""
        from hephaestus.automation.models import IssueState

        with patch(
            "hephaestus.automation.planner_state.prefetch_issue_states",
            return_value={123: IssueState.CLOSED},
        ):
            result = planner._filter_issues()

        assert result == []

    def test_filter_passes_open_issues_through(self, planner: Planner) -> None:
        """Open issues survive the pre-pass and are returned for the worker pool."""
        from hephaestus.automation.models import IssueState

        with patch(
            "hephaestus.automation.planner_state.prefetch_issue_states",
            return_value={123: IssueState.OPEN},
        ):
            result = planner._filter_issues()

        assert result == [123]


class TestPlanReviewLoopProtocolSubstitutability:
    """Verify PlanReviewLoop depends on PlannerHost protocol, not concrete Planner.

    This ensures the loop is genuinely substitutable — it can be instantiated
    with any object that implements the PlannerHost protocol, not just a
    Planner instance.
    """

    def test_loop_runs_with_protocol_host(self) -> None:
        """PlanReviewLoop must work with a minimal fake host (not a real Planner).

        This is the core proof that the Protocol injection works: the loop
        doesn't depend on the concrete Planner class, only the interface.
        """
        from unittest.mock import MagicMock

        from hephaestus.automation.planner_review_loop import PlanReviewLoop

        # Create a minimal fake host that satisfies PlannerHost protocol
        fake_host = MagicMock()
        fake_host.options = MagicMock()
        fake_host.options.enable_advise = False
        fake_host.status_tracker = MagicMock()
        fake_host._run_advise = MagicMock(return_value="advise")
        fake_host._generate_plan = MagicMock(return_value="plan")
        fake_host._capture_planner_learnings = MagicMock(return_value="learnings")
        fake_host._run_plan_review = MagicMock(return_value=_go_review())
        fake_host._call_claude = MagicMock(return_value="result")

        # Instantiate PlanReviewLoop with the fake host (not a Planner)
        loop = PlanReviewLoop(fake_host)

        # Verify the loop can run without error
        with patch(
            "hephaestus.automation.planner_review_loop.gh_issue_json",
            return_value={"title": "Test", "body": "Description"},
        ):
            plan, _review, iterations, verdict_is_go = loop.run(123, slot_id=0)

        # Verify the loop called the host's methods
        assert fake_host._generate_plan.called
        assert fake_host._capture_planner_learnings.called
        assert fake_host._run_plan_review.called
        assert plan == "plan"
        assert iterations == 1
        assert verdict_is_go is True
