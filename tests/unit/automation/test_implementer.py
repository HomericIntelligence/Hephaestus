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
from hephaestus.automation.models import ImplementerOptions


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
