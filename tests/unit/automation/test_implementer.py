"""Tests for hephaestus.automation.implementer.

Smoke-level coverage of the CLI surface: argument parser shape and
top-level help. Deeper behavioral tests for IssueImplementer live next
to the workflow they exercise; here we guard against regressions in
the public CLI contract that other repos shell out to.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation import implementer
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
# #551 — implementer must gate on APPROVED plan-review verdict
# ---------------------------------------------------------------------------


class TestPlanReviewVerdictGate:
    """#551: _implement_issue must skip when latest plan-review is not APPROVED.

    Behaviour matrix:

    +----------------------------+-----------+-------------------------+
    | Latest plan-review verdict | Implement?| ``plan_review_not_approved``|
    +============================+===========+=========================+
    | APPROVED                   | yes       | False                   |
    | REVISE                     | no (skip) | True                    |
    | BLOCK                      | no (skip) | True                    |
    | missing (no review yet)    | no (skip) | True                    |
    | NOGO-exhausted plan        | no (skip) | True                    |
    +----------------------------+-----------+-------------------------+

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
            patch(
                "hephaestus.automation.implementer.is_plan_review_approved",
                return_value=gate_return,
            ),
            # Everything past the gate — only reached when gate returns True.
            patch.object(impl, "_run_claude_code", return_value="sess-1"),
            patch.object(
                impl,
                "_run_impl_review_loop",
                return_value=(1, "GO", "A"),
            ),
            patch.object(impl, "_finalize_pr", return_value=999),
            patch.object(impl, "_run_post_pr_followup"),
            patch(
                "hephaestus.automation.implementer.fetch_issue_info",
                return_value=MagicMock(title="t", body="b"),
            ),
        ):
            return impl._implement_issue(1)

    def test_approved_verdict_proceeds_to_implementation(
        self, impl: IssueImplementer, tmp_path: Path
    ) -> None:
        result = self._drive_to_gate(impl, tmp_path, gate_return=True)
        assert result.success is True
        assert result.plan_review_not_approved is False
        assert result.pr_number == 999

    def test_revise_verdict_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        # Mirror: REVISE is one of the non-APPROVED states the gate must reject.
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True  # not a failure — retried next loop
        assert result.plan_review_not_approved is True
        assert result.pr_number is None

    def test_block_verdict_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        # is_plan_review_approved returns False for BLOCK just like REVISE; the
        # gate handles both uniformly. Asserting both paths makes the contract
        # explicit and protects against accidental ``if verdict == "BLOCK"``
        # special-casing creeping in.
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.plan_review_not_approved is True

    def test_missing_review_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        # No plan-review posted yet → is_plan_review_approved returns False.
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True
        assert result.plan_review_not_approved is True

    def test_nogo_exhausted_plan_skips(self, impl: IssueImplementer, tmp_path: Path) -> None:
        """Skip NOGO-exhausted plans (planner.py:692-700).

        These still start with "# Implementation Plan", so ``_has_plan``
        returns True, but the plan-reviewer will mark them BLOCK (or there
        is no review yet). The new gate must skip them regardless. See
        #551 acceptance #4.
        """
        result = self._drive_to_gate(impl, tmp_path, gate_return=False)
        assert result.success is True
        assert result.plan_review_not_approved is True

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
                "hephaestus.automation.implementer.is_plan_review_approved",
                return_value=False,
            ),
        ):
            result = impl._implement_issue(1)

        has_plan.assert_called_once_with(1)
        gen_plan.assert_called_once_with(1)
        assert result.success is True
        assert result.plan_review_not_approved is True

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
                "hephaestus.automation.implementer.is_plan_review_approved",
                return_value=False,
            ),
        ):
            result = impl._implement_issue(1)

        assert result.plan_review_not_approved is True
        assert ImplementationPhase.WAITING_FOR_PLAN_REVIEW in captured_phases


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
