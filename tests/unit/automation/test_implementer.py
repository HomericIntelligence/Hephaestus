"""Tests for hephaestus.automation.implementer.

Smoke-level coverage of the CLI surface: argument parser shape and
top-level help. Deeper behavioral tests for IssueImplementer live next
to the workflow they exercise; here we guard against regressions in
the public CLI contract that other repos shell out to.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import implementer
from hephaestus.automation._review_utils import DEFAULT_STATE_DIR
from hephaestus.automation.implementer import (
    _CLAUDE_IMPL_TIMEOUT,
    IssueImplementer,
)
from hephaestus.automation.models import ImplementerOptions, WorkerResult


class TestModuleSurface:
    """Tests for module surface."""

    def test_main_callable(self) -> None:
        assert callable(implementer.main)

    def test_implementer_class_exposed(self) -> None:
        assert hasattr(implementer, "IssueImplementer")


class TestParseArgs:
    """Tests for parse args."""

    def test_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl"])
        args = implementer._parse_args()
        assert args.epic is None
        assert args.issues is None
        assert args.dry_run is False

    def test_explicit_issues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--issues", "1", "2", "3", "--dry-run"])
        args = implementer._parse_args()
        assert args.issues == [1, 2, 3]
        assert args.dry_run is True

    def test_epic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["impl", "--epic", "42"])
        args = implementer._parse_args()
        assert args.epic == 42


class TestHelpInvocation:
    """Tests for help invocation."""

    def test_module_help_exits_zero(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "hephaestus.automation.implementer", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0
        assert "usage" in (result.stdout + result.stderr).lower()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dry_run_implementer(tmp_path: Path) -> IssueImplementer:
    """IssueImplementer configured for dry-run with temp repo root."""
    options = ImplementerOptions(
        epic_number=0,
        issues=[1],
        max_workers=1,
        skip_closed=False,
        auto_merge=False,
        dry_run=True,
        enable_learn=False,
        enable_follow_up=False,
        enable_ui=False,
    )
    with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
        return IssueImplementer(options)


class TestIssueImplementerDynamicDelegates:
    """Regression coverage for low-risk legacy helper lookup."""

    PHASE_DELEGATES: ClassVar[frozenset[str]] = frozenset(
        {
            "_parse_follow_up_items",
            "_can_resume_state_session",
            "_run_follow_up_issues",
            "_learn_needs_rerun",
            "_rerun_failed_learns",
            "_run_learn",
            "_run_advise_as_implementer_turn",
            "_run_claude_impl_session",
            "_run_codex_code",
            "_save_review_log",
            "_load_review_iteration_state",
            # #1438/#1439: former explicit pure-forward shims, now resolved here
            "_finalize_pr",
            "_run_post_pr_followup",
            "_implement_issue",
            "_has_plan",
            "_generate_plan",
            "_run_advise",
            "_run_impl_review_loop",
            "_run_impl_review_step",
            "_run_address_review_step",
            "_resume_impl_with_feedback",
            "_run_impl_review",
            "_collect_diff",
            "_collect_changed_files",
            "_save_review_iteration_state",
            "_run_tests_in_worktree",
            "_run_claude_code",
            "_ensure_pr_created",
        }
    )
    STATE_DELEGATES: ClassVar[dict[str, str]] = {
        "_get_or_create_state": "get_or_create",
        "_get_state": "get",
        "_save_state": "save",
        "_load_state": "load_all",
    }

    def test_dynamic_delegate_tables_are_exact(self) -> None:
        assert IssueImplementer._PHASE_RUNNER_DYNAMIC_DELEGATES == self.PHASE_DELEGATES
        assert IssueImplementer._STATE_MANAGER_DYNAMIC_DELEGATES == self.STATE_DELEGATES

    @pytest.mark.parametrize("name", sorted(PHASE_DELEGATES))
    def test_phase_runner_dynamic_delegate_resolves_to_runner(
        self, dry_run_implementer: IssueImplementer, name: str
    ) -> None:
        assert getattr(dry_run_implementer, name) == getattr(dry_run_implementer.phase_runner, name)

    @pytest.mark.parametrize(("name", "manager_method"), sorted(STATE_DELEGATES.items()))
    def test_state_dynamic_delegate_resolves_to_state_manager(
        self, dry_run_implementer: IssueImplementer, name: str, manager_method: str
    ) -> None:
        assert getattr(dry_run_implementer, name) == getattr(
            dry_run_implementer.state_manager, manager_method
        )

    @pytest.mark.parametrize(
        "name",
        sorted(
            PHASE_DELEGATES
            | set(STATE_DELEGATES)
            | {"_commit_changes", "_create_pr", "_print_summary"}
        ),
    )
    def test_removed_delegates_are_not_class_methods(self, name: str) -> None:
        assert name not in IssueImplementer.__dict__

    def test_dynamic_commit_changes_passes_selected_agent(
        self, dry_run_implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        dry_run_implementer.options.agent = "codex"
        with patch("hephaestus.automation.implementer.commit_changes") as commit:
            dry_run_implementer._commit_changes(7, tmp_path)
        commit.assert_called_once_with(7, tmp_path, "codex", git_message_timeout=1200)

    def test_dynamic_create_pr_preserves_legacy_arguments(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        dry_run_implementer.options.agent = "codex"
        with patch("hephaestus.automation.implementer.create_pr", return_value=42) as create:
            assert dry_run_implementer._create_pr(7, "7-auto-impl") == 42
        create.assert_called_once_with(
            7, "7-auto-impl", auto_merge=False, agent="codex", git_message_timeout=1200
        )

    def test_dynamic_summary_printer_constructs_at_call_time(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        results = {1: WorkerResult(issue_number=1, success=True)}
        with patch("hephaestus.automation.implementer.ImplementationSummaryPrinter") as printer:
            dry_run_implementer._print_summary(results)
        printer.assert_called_once_with(dry_run_implementer.worktree_manager)
        printer.return_value.print.assert_called_once_with(results)

    def test_patch_object_still_intercepts_dynamic_delegate(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        with patch.object(dry_run_implementer, "_save_state") as save_state:
            dry_run_implementer._save_state(MagicMock())
        save_state.assert_called_once()
        assert "_save_state" not in dry_run_implementer.__dict__

    def test_patch_object_intercepts_former_shim(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        # #1439: _run_advise was an explicit shim; now resolves via __getattr__.
        assert "_run_advise" not in type(dry_run_implementer).__dict__
        with patch.object(dry_run_implementer, "_run_advise", return_value="x") as advise:
            assert dry_run_implementer._run_advise(1, "t", "b") == "x"
        advise.assert_called_once_with(1, "t", "b")
        assert "_run_advise" not in dry_run_implementer.__dict__

    def test_instance_assignment_still_shadows_dynamic_commit_delegate(
        self, dry_run_implementer: IssueImplementer, tmp_path: Path
    ) -> None:
        assigned_commit = MagicMock()
        dry_run_implementer._commit_changes = assigned_commit  # type: ignore[attr-defined]
        dry_run_implementer._commit_changes(7, tmp_path)
        assigned_commit.assert_called_once_with(7, tmp_path)

    def test_unknown_private_attribute_raises_attribute_error(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        with pytest.raises(
            AttributeError,
            match="IssueImplementer object has no attribute '_sav_state'",
        ):
            missing_name = "_sav_state"
            getattr(dry_run_implementer, missing_name)


def test_default_state_dir_uses_canonical_path(dry_run_implementer: IssueImplementer) -> None:
    """IssueImplementer creates the shared default automation state directory."""
    assert dry_run_implementer.state_dir == dry_run_implementer.repo_root / DEFAULT_STATE_DIR
    assert dry_run_implementer.state_dir.is_dir()


# ---------------------------------------------------------------------------
# Issue #371 — dry-run must not create real worktrees or branches
# ---------------------------------------------------------------------------


class TestDryRunNoWorktree:
    """In --dry-run mode, WorktreeManager.create_worktree must NOT be called (#371)."""

    def test_create_worktree_not_called_in_dry_run(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        """_implement_issue dry-run path must return success WITHOUT touching WorktreeManager."""
        with patch.object(dry_run_implementer.worktree_manager, "create_worktree") as mock_create:
            result = dry_run_implementer._implement_issue(1)

        (
            mock_create.assert_not_called(),
            ("create_worktree was called in dry-run mode — real worktree would have been created"),
        )
        assert result.success is True
        # Worktree path should be None in dry-run (never created)
        assert result.worktree_path is None

    def test_dry_run_result_carries_branch_name(
        self, dry_run_implementer: IssueImplementer
    ) -> None:
        """Even in dry-run mode the result must carry the expected branch name."""
        with patch.object(dry_run_implementer.worktree_manager, "create_worktree"):
            result = dry_run_implementer._implement_issue(1)

        assert result.branch_name == "1-auto-impl"


# ---------------------------------------------------------------------------
# A2-008 — module-level _CLAUDE_IMPL_TIMEOUT constant
# ---------------------------------------------------------------------------


class TestClaudeImplTimeoutConstant:
    """A2-008: _CLAUDE_IMPL_TIMEOUT module-level constant must exist and be 1800."""

    def test_constant_is_exposed(self) -> None:
        assert hasattr(implementer, "_CLAUDE_IMPL_TIMEOUT")

    def test_constant_value(self) -> None:
        assert _CLAUDE_IMPL_TIMEOUT == 1800


# ---------------------------------------------------------------------------
# A2-004 — _run_tests_in_worktree pre-PR gate
# ---------------------------------------------------------------------------


class TestRunTestsInWorktree:
    """A2-004: _run_tests_in_worktree must return True/False based on subprocess exit."""

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            run_pre_pr_tests=True,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def test_returns_true_on_zero_exit(self, impl: IssueImplementer, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1 passed"
        mock_result.stderr = ""
        with patch("hephaestus.automation.implementer.subprocess.run", return_value=mock_result):
            assert impl._run_tests_in_worktree(tmp_path, issue_number=1) is True

    def test_returns_false_on_nonzero_exit(self, impl: IssueImplementer, tmp_path: Path) -> None:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "FAILED"
        mock_result.stderr = "AssertionError"
        with patch("hephaestus.automation.implementer.subprocess.run", return_value=mock_result):
            assert impl._run_tests_in_worktree(tmp_path, issue_number=1) is False

    def test_returns_false_on_timeout(self, impl: IssueImplementer, tmp_path: Path) -> None:
        with patch(
            "hephaestus.automation.implementer.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["pixi"], 600),
        ):
            assert impl._run_tests_in_worktree(tmp_path, issue_number=1) is False

    def test_run_pre_pr_tests_false_skips_test_run(self, tmp_path: Path) -> None:
        """When run_pre_pr_tests=False, _run_tests_in_worktree must NOT be called."""
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            run_pre_pr_tests=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            impl = IssueImplementer(options)

        with patch.object(impl, "_run_tests_in_worktree") as mock_tests:
            # Patch enough to prevent real subprocess calls
            with (
                patch.object(impl, "_ensure_pr_created", return_value=42),
                patch.object(impl, "_save_state"),
                patch(
                    "hephaestus.automation._pr_create_phase._has_uncommitted_changes",
                    return_value=False,
                ),
            ):
                impl._finalize_pr(
                    issue_number=1,
                    branch_name="1-auto-impl",
                    worktree_path=tmp_path,
                    state=MagicMock(),
                    slot_id=None,
                )

        mock_tests.assert_not_called()


# ---------------------------------------------------------------------------
# #551 — implementer must gate on GO plan-review verdict
# ---------------------------------------------------------------------------


class TestPlanReviewVerdictGate:
    """#551: _implement_issue must skip when latest plan-review is not GO.

    Behaviour matrix:

    +----------------------------+-----------+-----------------------+
    | Latest plan-review verdict | Implement?| ``plan_review_not_go``|
    +============================+===========+=======================+
    | GO                         | yes       | False                 |
    | NOGO                       | no (skip) | True                  |
    | missing (no review yet)    | no (skip) | True                  |
    | NOGO-exhausted plan        | no (skip) | True                  |
    +----------------------------+-----------+-----------------------+

    The "no plan at all" path is covered by the pre-existing ``_has_plan``
    branch — the planner runs before the gate, and the gate only fires once
    a plan comment exists.
    """

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def _drive_to_gate(
        self,
        impl: IssueImplementer,
        tmp_path: Path,
        gate_return: bool,
    ) -> WorkerResult:
        """Run ``_implement_issue(1)`` past worktree creation and the gate.

        All side-effects beyond the gate (Claude invocation, review loop,
        PR creation, follow-up) are patched out — we only assert what the
        gate itself does.
        """
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)

        with (
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_has_plan", return_value=True),
            patch.object(impl, "_save_state"),
            # The existing-PR skip guard runs before this gate; no PR by default.
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=gate_return,
            ),
            # Everything past the gate — only reached when gate returns True.
            patch.object(impl, "_run_advise"),
            patch.object(impl, "_run_claude_code", return_value="sess-1"),
            patch.object(
                impl,
                "_run_impl_review_loop",
                return_value=(1, "GO", "A"),
            ),
            patch.object(impl, "_finalize_pr", return_value=999),
            patch.object(impl, "_run_post_pr_followup"),
            patch("hephaestus.automation.implementer_phase_runner.ensure_pr_auto_merge_deferred"),
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go"),
            patch("hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"),
            patch(
                "hephaestus.automation.implementer_phase_runner.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
        ):
            return impl._implement_issue(1)

    def test_go_verdict_proceeds_to_implementation(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        result = self._drive_to_gate(impl, tmp_path, gate_return=True)
        assert result.success is True
        assert result.plan_review_not_go is False
        assert result.pr_number == 999

    def test_nogo_verdict_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        # NOGO is the single non-GO verdict the gate must reject (it subsumes
        # the former REVISE and BLOCK states, which were never behaviorally
        # distinct — the gate only ever asked "is it GO").
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True  # not a failure — retried next loop
        assert result.plan_review_not_go is True
        assert result.pr_number is None

    def test_missing_review_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        # No plan-review posted yet → is_plan_review_go returns False.
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True
        assert result.plan_review_not_go is True

    def test_nogo_exhausted_plan_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Skip NOGO-exhausted plans (planner.py:692-700).

        These still start with "# Implementation Plan", so ``_has_plan``
        returns True, but the plan-reviewer will mark them NOGO (or there
        is no review yet). The new gate must skip them regardless. See
        #551 acceptance #4.
        """
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True
        assert result.plan_review_not_go is True

    def test_no_plan_path_unchanged(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Sanity-check the pre-existing ``_has_plan`` branch.

        When no plan comment exists, the planner is invoked and the gate
        is then evaluated (returning False here because the planner just
        posted a plan but no review exists yet).
        """
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        with (
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_has_plan", return_value=False) as has_plan,
            patch.object(impl, "_generate_plan") as gen_plan,
            patch.object(impl, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=False,
            ),
        ):
            result = impl._implement_issue(1)

        has_plan.assert_called_once_with(1)
        gen_plan.assert_called_once_with(1)
        assert result.success is True
        assert result.plan_review_not_go is True

    def test_skip_uses_waiting_phase(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Skipping must record WAITING_FOR_PLAN_REVIEW in state.phase.

        The orchestrator (and any observer reading state files) needs to
        tell a deferred issue apart from a completed one.
        """
        from hephaestus.automation.models import ImplementationPhase

        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)

        captured_phases: list[ImplementationPhase] = []

        def _record_save(state: object, *args: object, **kwargs: object) -> None:
            captured_phases.append(state.phase)  # type: ignore[attr-defined]

        with (
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_has_plan", return_value=True),
            patch.object(impl, "_save_state", side_effect=_record_save),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=False,
            ),
        ):
            result = impl._implement_issue(1)

        assert result.plan_review_not_go is True
        assert ImplementationPhase.WAITING_FOR_PLAN_REVIEW in captured_phases


# ---------------------------------------------------------------------------
# Skip implementation when an open PR already exists for the issue
# ---------------------------------------------------------------------------


class TestExistingPrEntersReviewLoop:
    """``_implement_issue`` drives an already-open PR through the review loop.

    Replaces the old "skip existing PRs entirely" behavior: a pre-existing PR
    is reviewed (and fixed on NOGO) so it can earn the ``state:implementation-go``
    label that drive-green requires — otherwise it deadlocks green-but-unmergeable.
    The plan/implement steps are still skipped (the PR is already open), and the
    worktree is hard-reset to ``origin/<branch>`` first to avoid clobbering
    pushed work. A PR already carrying a terminal label short-circuits.
    """

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def test_existing_pr_without_label_runs_review_and_labels_go(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)

        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=777,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, False),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="1-auto-impl",
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as sync,
            patch.object(
                impl.worktree_manager, "create_worktree", return_value=worktree_path
            ) as create_wt,
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch.object(impl, "_save_state"),
            patch.object(impl, "_has_plan") as has_plan,
            patch.object(impl, "_run_claude_code") as run_agent,
            patch.object(impl, "_finalize_pr") as finalize_pr,
            patch.object(impl, "_run_impl_review_loop", return_value=(1, "GO", "A")) as review_loop,
            patch(
                "hephaestus.automation.implementer_phase_runner.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
            patch.object(impl, "_run_advise"),
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go") as mark_no_go,
            patch("hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"),
        ):
            result = impl._implement_issue(1)

        assert result.success is True
        assert result.already_has_pr is True
        assert result.pr_number == 777
        # Worktree IS created and synced; the implement agent and PR creation
        # are NOT (the PR already exists).
        create_wt.assert_called_once()
        sync.assert_called_once()
        run_agent.assert_not_called()
        has_plan.assert_not_called()
        finalize_pr.assert_not_called()
        # Review loop ran with no initial session_id against the existing PR.
        review_loop.assert_called_once()
        assert review_loop.call_args.kwargs["session_id"] is None
        assert review_loop.call_args.kwargs["pr_number"] == 777
        # GO verdict labels the PR implementation-GO.
        mark_go.assert_called_once_with(777)
        mark_no_go.assert_not_called()

    def test_existing_pr_with_go_label_short_circuits(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=777,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(True, False),
            ),
            patch.object(impl.worktree_manager, "create_worktree") as create_wt,
            patch.object(impl, "_save_state"),
            patch.object(impl, "_run_impl_review_loop") as review_loop,
        ):
            result = impl._implement_issue(1)

        assert result.success is True
        assert result.already_has_pr is True
        assert result.pr_number == 777
        # Already settled — no worktree, no review.
        create_wt.assert_not_called()
        review_loop.assert_not_called()

    def test_existing_pr_with_no_go_label_re_enters_review_loop(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """A NO-GO PR is NOT settled — it re-enters implement + review to earn GO.

        Regression guard for the bug where ``has_go or has_no_go`` short-circuited
        NO-GO PRs identically to GO, leaving them untouched every loop.
        """
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=777,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, True),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="708-auto-impl",
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"
            ) as sync,
            patch.object(
                impl.worktree_manager, "create_worktree", return_value=worktree_path
            ) as create_wt,
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch.object(impl, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
            patch.object(impl, "_run_advise"),
            patch.object(impl, "_run_impl_review_loop", return_value=(2, "GO", "A")) as review_loop,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go"),
            patch("hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"),
        ):
            result = impl._implement_issue(1)

        assert result.success is True
        assert result.already_has_pr is True
        # NO-GO re-enters the loop: worktree prepped + review loop run, and on a
        # GO verdict from the re-review the PR is re-labeled GO (no longer skipped).
        create_wt.assert_called_once()
        review_loop.assert_called_once()
        mark_go.assert_called_once_with(777)
        # Regression guard: the worktree is prepared/synced on the PR's REAL head
        # branch (708-auto-impl from get_pr_head_branch), NOT the assumed
        # "1-auto-impl" — otherwise git fetch fails with exit 128.
        assert create_wt.call_args.args[1] == "708-auto-impl"
        assert sync.call_args.args[1] == "708-auto-impl"

    def test_existing_pr_review_no_go_marks_no_go(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)

        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=777,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.pr_has_implementation_state_label",
                return_value=(False, False),
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.get_pr_head_branch",
                return_value="1-auto-impl",
            ),
            patch("hephaestus.automation.implementer_phase_runner.sync_worktree_to_remote_branch"),
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_clean_working_tree",
                return_value=True,
            ),
            patch.object(impl, "_save_state"),
            patch.object(impl, "_run_claude_code") as run_agent,
            patch.object(impl, "_run_impl_review_loop", return_value=(3, "NOGO", "D")),
            patch(
                "hephaestus.automation.implementer_phase_runner.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
            patch.object(impl, "_run_advise"),
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go") as mark_go,
            patch("hephaestus.automation._review_phase.mark_pr_implementation_no_go") as mark_no_go,
        ):
            result = impl._implement_issue(1)

        assert result.success is True
        assert result.already_has_pr is True
        run_agent.assert_not_called()
        mark_no_go.assert_called_once_with(777)
        mark_go.assert_not_called()

    def test_no_existing_pr_proceeds(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """When no PR exists, the guard is transparent and work proceeds."""
        worktree_path = tmp_path / "worktree"
        worktree_path.mkdir(exist_ok=True)

        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_has_plan", return_value=True),
            patch.object(impl, "_save_state"),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=True,
            ),
            patch.object(impl, "_run_advise"),
            patch.object(impl, "_run_claude_code", return_value="sess-1"),
            patch.object(impl, "_run_impl_review_loop", return_value=(1, "GO", "A")),
            patch.object(impl, "_finalize_pr", return_value=999),
            patch("hephaestus.automation.implementer_phase_runner.ensure_pr_auto_merge_deferred"),
            patch("hephaestus.automation._review_phase.mark_pr_implementation_go"),
            patch("hephaestus.automation._review_phase.enable_auto_merge_after_implementation_go"),
            patch.object(impl, "_run_post_pr_followup"),
            patch(
                "hephaestus.automation.implementer_phase_runner.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
        ):
            result = impl._implement_issue(1)

        assert result.success is True
        assert result.already_has_pr is False
        assert result.pr_number == 999


# ---------------------------------------------------------------------------
# #574 — run() must short-circuit cleanly when there are no issues and no epic
# ---------------------------------------------------------------------------


class TestRunWithNothingToImplement:
    """#574 regression: implementer must short-circuit on empty input.

    When the CLI's auto-discovery returns zero open issues AND no epic is
    specified, ``IssueImplementer.run()`` must NOT fall through to
    ``load_epic(0)`` (which would retry-storm against ``gh issue view 0``).
    Mirrors the planner's ``if not issues_to_plan: warn; return {}`` pattern.
    """

    def test_run_with_no_issues_and_no_epic_short_circuits(self, tmp_path: Path) -> None:
        """No issues and epic=0 → run() returns {} after a warning, never calls load_epic."""
        options = ImplementerOptions(
            issues=[],
            epic_number=0,
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            health_check=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            impl = IssueImplementer(options)

        with patch.object(impl.resolver, "load_epic") as mock_load_epic:
            result = impl.run()

        assert result == {}
        mock_load_epic.assert_not_called()

    def test_run_with_issues_still_proceeds(self, tmp_path: Path) -> None:
        """Sanity check: the short-circuit must NOT fire when issues are present."""
        options = ImplementerOptions(
            issues=[42],
            epic_number=0,
            dry_run=True,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            health_check=False,
            analyze_only=True,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            impl = IssueImplementer(options)

        with (
            patch.object(impl, "_load_issues") as mock_load_issues,
            patch.object(impl.resolver, "detect_cycles"),
            patch.object(impl, "_analyze_dependencies", return_value={}),
        ):
            impl.run()

        mock_load_issues.assert_called_once_with([42])

    def test_run_with_epic_but_no_issues_still_proceeds(self, tmp_path: Path) -> None:
        """Sanity check: the short-circuit must NOT fire when an epic is given."""
        options = ImplementerOptions(
            issues=[],
            epic_number=123,
            dry_run=True,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            health_check=False,
            analyze_only=True,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            impl = IssueImplementer(options)

        with (
            patch.object(impl.resolver, "load_epic") as mock_load_epic,
            patch.object(impl.resolver, "detect_cycles"),
            patch.object(impl, "_analyze_dependencies", return_value={}),
        ):
            impl.run()

        mock_load_epic.assert_called_once_with(123)


# ---------------------------------------------------------------------------
# Issue #1083 — `state:skip` label gates an issue out of all phases
# ---------------------------------------------------------------------------


class TestStateSkipLabel:
    """``state:skip`` on an issue normally makes _load_issues skip it entirely.

    Mirrors the existing ``skip_closed`` gate: a skipped issue is marked
    completed in the resolver (so dependents are not blocked) and is never
    added to the work graph, except for the stale approved-plan/no-PR recovery
    path covered below.
    """

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            epic_number=0,
            issues=[1],
            max_workers=1,
            skip_closed=True,
            auto_merge=False,
            dry_run=True,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def test_issue_with_state_skip_label_is_skipped(self, impl: IssueImplementer) -> None:
        from hephaestus.automation.models import IssueInfo

        skipped = IssueInfo(number=42, title="t", labels=["state:skip"])
        with (
            patch(
                "hephaestus.automation.github_api.prefetch_issue_states",
                return_value={},
            ),
            patch(
                "hephaestus.automation.implementer.fetch_issue_info",
                return_value=skipped,
            ) as mock_fetch,
            patch.object(impl.resolver, "add_issue") as mock_add,
        ):
            impl._load_issues([42])

        # Skipped issue must not enter the work graph but must count as done.
        mock_fetch.assert_called_once_with(42)
        mock_add.assert_not_called()
        assert 42 in impl.resolver.completed

    def test_issue_without_state_skip_label_is_loaded(self, impl: IssueImplementer) -> None:
        from hephaestus.automation.models import IssueInfo

        normal = IssueInfo(number=43, title="t", labels=["state:plan-go"])
        with (
            patch(
                "hephaestus.automation.github_api.prefetch_issue_states",
                return_value={},
            ),
            patch(
                "hephaestus.automation.implementer.fetch_issue_info",
                return_value=normal,
            ),
            patch.object(impl.resolver, "_load_dependencies"),
            patch.object(impl.resolver, "add_issue") as mock_add,
        ):
            impl._load_issues([43])

        mock_add.assert_called_once_with(normal)
        assert 43 not in impl.resolver.completed

    def test_plan_go_skip_with_existing_pr_still_skips(self, impl: IssueImplementer) -> None:
        # #1576: state:skip is ABSOLUTE and operator-only — no auto-recovery,
        # even with state:plan-go + an open PR. The label is honored verbatim.
        from hephaestus.automation.models import IssueInfo

        skipped = IssueInfo(number=44, title="t", labels=["state:skip", "state:plan-go"])
        with (
            patch(
                "hephaestus.automation.github_api.prefetch_issue_states",
                return_value={},
            ),
            patch(
                "hephaestus.automation.implementer.fetch_issue_info",
                return_value=skipped,
            ),
            patch.object(impl.resolver, "add_issue") as mock_add,
        ):
            impl._load_issues([44])

        mock_add.assert_not_called()
        assert 44 in impl.resolver.completed

    def test_plan_go_skip_without_pr_also_skips_no_recovery(self, impl: IssueImplementer) -> None:
        # #1576: removed the no-PR stale-skip recovery escape hatch — a
        # state:skip issue is ALWAYS skipped, even plan-go with no PR. It is the
        # operator's responsibility to remove the label between runs.
        from hephaestus.automation.models import IssueInfo

        skipped = IssueInfo(number=45, title="t", labels=["state:skip", "state:plan-go"])
        with (
            patch(
                "hephaestus.automation.github_api.prefetch_issue_states",
                return_value={},
            ),
            patch(
                "hephaestus.automation.implementer.fetch_issue_info",
                return_value=skipped,
            ),
            patch.object(impl.resolver, "add_issue") as mock_add,
        ):
            impl._load_issues([45])

        mock_add.assert_not_called()
        assert 45 in impl.resolver.completed


class TestNoChangesProducedAppliesStateSkip:
    """When the agent produces no commits vs main, the work already landed.

    ``_implement_issue`` must: (1) apply ``state:skip`` to the issue so future
    loops don't re-attempt it, (2) return ``WorkerResult(success=True)`` so the
    issue does NOT count as a failure and inflate the exit code.
    """

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def test_no_changes_returns_success_and_applies_state_skip(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        worktree_path = tmp_path / "wt"
        worktree_path.mkdir()
        no_changes_error = RuntimeError(
            "No changes produced for issue HomericIntelligence/ProjectHephaestus#736: "
            "branch '736-auto-impl' has no commits vs 'main'. "
            "Skipping PR creation (the implementation session made no net change)."
        )
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_merged_closing_pr",
                return_value=700,
            ),
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_save_state"),
            patch.object(impl, "_has_plan", return_value=True),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info"),
            patch.object(impl, "_run_advise"),
            patch.object(impl, "_run_claude_code", return_value="session-id"),
            patch.object(impl, "_finalize_pr", side_effect=no_changes_error),
            patch(
                "hephaestus.automation.implementer_phase_runner.gh_issue_add_labels"
            ) as mock_label,
        ):
            result = impl.phase_runner._implement_issue(736)

        assert result.success is True
        assert result.issue_number == 736
        mock_label.assert_called_once_with(736, ["state:skip"])

    def test_empty_branch_without_merged_pr_is_failure(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        """A 0-commit branch with NO merged closing PR is a stale artifact.

        A killed/aborted run leaves an empty ``{issue}-auto-impl`` branch;
        declaring the work "already merged" silently skips real work
        (observed live: ProjectOdyssey#5520). It must fail — without the
        ``state:skip`` label — so a retry re-implements from scratch.
        """
        worktree_path = tmp_path / "wt"
        worktree_path.mkdir()
        no_changes_error = RuntimeError(
            "No changes produced for issue HomericIntelligence/ProjectHephaestus#736: "
            "branch '736-auto-impl' has no commits vs 'main'."
        )
        with (
            patch(
                "hephaestus.automation.implementer_phase_runner.find_pr_for_issue",
                return_value=None,
            ),
            patch(
                "hephaestus.automation.implementer_phase_runner.find_merged_closing_pr",
                return_value=None,
            ),
            patch.object(impl.worktree_manager, "create_worktree", return_value=worktree_path),
            patch.object(impl, "_save_state"),
            patch.object(impl, "_has_plan", return_value=True),
            patch(
                "hephaestus.automation.implementer_phase_runner.is_plan_review_go",
                return_value=True,
            ),
            patch("hephaestus.automation.implementer_phase_runner.fetch_issue_info"),
            patch.object(impl, "_run_advise"),
            patch.object(impl, "_run_claude_code", return_value="session-id"),
            patch.object(impl, "_finalize_pr", side_effect=no_changes_error),
            patch(
                "hephaestus.automation.implementer_phase_runner.gh_issue_add_labels"
            ) as mock_label,
        ):
            result = impl.phase_runner._implement_issue(736)

        assert result.success is False
        assert "stale empty branch" in str(result.error)
        mock_label.assert_not_called()


# ---------------------------------------------------------------------------
# Advise compatibility wrapper
# ---------------------------------------------------------------------------


class TestAdviseAsImplementerTurn:
    """_run_advise_as_implementer_turn now delegates to the shared advise path."""

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
            enable_advise=True,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    def test_delegates_to_shared_advise_path(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Claude and Codex advise use the same selected-skill context behavior."""
        worktree_path = tmp_path / "build" / ".worktrees" / "issue-1"
        worktree_path.mkdir(parents=True)

        with patch.object(
            impl.phase_runner.implement_phase, "_run_advise", return_value="selected skills"
        ) as run_advise:
            result = impl._run_advise_as_implementer_turn(
                issue_number=1,
                issue_title="Fix the widget",
                issue_body="It is broken",
                worktree_path=worktree_path,
            )

        assert result == "selected skills"
        run_advise.assert_called_once_with(1, "Fix the widget", "It is broken")

    def test_learn_passes_implementer_model(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """_run_learn forwards implementer_model() to run_learn, not the Haiku default."""
        from hephaestus.automation.claude_models import implementer_model

        captured: list[dict] = []

        def _fake_run_learn(*args: object, **kw: object) -> bool:
            captured.append({"args": args, "kwargs": kw})
            return True

        with patch(
            "hephaestus.automation._followup_phase.run_learn",
            side_effect=_fake_run_learn,
        ):
            impl._run_learn("sess-1", tmp_path, issue_number=1)

        assert captured, "Expected run_learn to be called"
        call_kw = captured[0]["kwargs"]
        assert "model" in call_kw, "_run_learn must pass model= to run_learn"
        assert call_kw["model"] == implementer_model(), (
            f"Expected implementer_model() but got {call_kw['model']!r}"
        )


# #715 — _has_plan and _fetch_plan_and_review must use prefix-match not substring
# ---------------------------------------------------------------------------------


class TestHasPlanPrefixMatch:
    """Regression coverage for #715 — _has_plan must NOT substring-match.

    A ``## 🔍 Plan Review`` comment quotes the plan and therefore contains
    ``## Implementation Plan`` and ``## Plan`` as substrings. The pre-#715
    implementation returned True on those, causing the implementer to
    treat its own prior plan-review as "the plan" (#455/#468/#484 bug
    class).
    """

    @pytest.fixture
    def impl(self, tmp_path: Path) -> IssueImplementer:
        # Mirror TestPlanReviewVerdictGate.impl (line 241-251): the fixture
        # is class-scoped throughout this module, never module-level, so this
        # class declares its own to stay self-contained.
        options = ImplementerOptions(
            issues=[1],
            dry_run=False,
            enable_learn=False,
            enable_follow_up=False,
            enable_ui=False,
        )
        with patch("hephaestus.automation.implementer.get_repo_root", return_value=tmp_path):
            return IssueImplementer(options)

    @staticmethod
    def _mock_gh_view(comments: list[dict[str, Any]]) -> Any:
        def mock_gh_view(*args: Any, **kwargs: Any) -> MagicMock:
            result = MagicMock()
            result.stdout = json.dumps({"comments": comments}).encode("utf-8")
            return result

        return mock_gh_view

    def test_plan_review_quoting_plan_heading_is_not_a_plan(self, impl: IssueImplementer) -> None:
        """_has_plan does not match a Plan Review comment that merely quotes the plan.

        Regression for #455/#468/#484/#715: a ``## 🔍 Plan Review`` body contains
        ``# Implementation Plan`` as substring when it quotes the plan, and
        substring matching caused the false positive. Prefix-matching (only at
        the start of the body) must be used instead.

        The fixture intentionally contains ONLY the Plan-Review comment (no
        genuine plan), so the OLD substring code would return ``True`` here —
        this test fails against the pre-fix code and guards the bug.
        """
        comments = [
            {
                "body": (
                    "## 🔍 Plan Review\n\n"
                    "The plan's # Implementation Plan section looks good.\n\n"
                    "Verdict: GO"
                )
            },
        ]

        with patch(
            "hephaestus.automation._plan_phase.gh_call",
            side_effect=self._mock_gh_view(comments),
        ):
            assert impl.phase_runner._has_plan(1) is False

    def test_real_plan_comment_is_detected(self, impl: IssueImplementer) -> None:
        """_has_plan returns True for a comment whose body starts with the marker.

        Covers the positive branch of the prefix check (#715): a body beginning
        with ``# Implementation Plan`` is a genuine plan.
        """
        comments = [
            {"body": "# Implementation Plan\n\nStep 1: Do something"},
        ]

        with patch(
            "hephaestus.automation._plan_phase.gh_call",
            side_effect=self._mock_gh_view(comments),
        ):
            assert impl.phase_runner._has_plan(1) is True
