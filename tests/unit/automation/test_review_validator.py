"""Tests for the report-only review-thread validator."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import patch

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation import review_validator


def _threads() -> list[dict[str, object]]:
    return [
        {"id": "T1", "path": "a.py", "line": 3, "body": "guard the null case"},
        {"id": "T2", "path": "b.py", "line": 7, "body": "rename for clarity"},
    ]


class TestReviewValidatorStructure:
    """Regression tests for the validator's bounded, report-only surface."""

    def test_validate_prior_comments_addressed_stays_under_line_cap(self) -> None:
        source = Path(review_validator.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        target = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "validate_prior_comments_addressed"
        )

        assert target.end_lineno is not None
        assert target.end_lineno - target.lineno + 1 <= 80

    def test_no_thread_resolution_mutation_exists(self) -> None:
        source = Path(review_validator.__file__).read_text(encoding="utf-8")

        assert "resolve" + "ReviewThread" not in source
        assert "gh_pr_" + "resolve_thread" not in source


class TestValidatePriorCommentsAddressed:
    """Open prior threads always remain human-owned merge gates."""

    def test_no_prior_threads_is_clean_noop(self, tmp_path: Path) -> None:
        reopened, is_clean, keys = review_validator.validate_prior_comments_addressed(
            pr_number=1,
            issue_number=1,
            worktree_path=tmp_path,
            prior_threads=[],
            diff_text="diff",
            agent="codex",
            iteration=1,
            state_dir=tmp_path,
        )

        assert (reopened, is_clean, keys) == ([], True, set())

    def test_dry_run_is_clean_noop(self, tmp_path: Path) -> None:
        with patch.object(review_validator, "_run_validation_session") as run:
            result = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="codex",
                iteration=1,
                state_dir=tmp_path,
                dry_run=True,
            )

        assert result == ([], True, set())
        run.assert_not_called()

    def test_addressed_threads_still_require_human_resolution(self, tmp_path: Path) -> None:
        with patch.object(review_validator, "_run_validation_session", return_value=([], [])):
            reopened, is_clean, keys = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="codex",
                iteration=1,
                state_dir=tmp_path,
                prior_reopened_keys={"legacy-key"},
            )

        assert (reopened, is_clean, keys) == ([], False, {"legacy-key"})

    def test_unaddressed_threads_are_report_only(self, tmp_path: Path) -> None:
        unaddressed = [{"thread_id": "T1", "path": "a.py", "line": 3, "detail": "still bad"}]
        with patch.object(
            review_validator,
            "_run_validation_session",
            return_value=(unaddressed, []),
        ):
            reopened, is_clean, _ = review_validator.validate_prior_comments_addressed(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="codex",
                iteration=1,
                state_dir=tmp_path,
            )

        assert (reopened, is_clean) == ([], False)


class TestRunValidationAndReconcile:
    """The internal adapter serializes facts but performs no GitHub write."""

    def test_returns_validator_report(self, tmp_path: Path) -> None:
        unaddressed = [{"thread_id": "T1", "detail": "missing guard"}]
        with patch.object(
            review_validator,
            "_run_validation_session",
            return_value=(unaddressed, []),
        ):
            result = review_validator._run_validation_and_reconcile(
                pr_number=1,
                issue_number=2,
                worktree_path=tmp_path,
                prior_threads=_threads(),
                diff_text="diff",
                agent="codex",
                iteration=3,
                state_dir=tmp_path,
                timeout=60,
            )

        assert result == unaddressed


class TestRunValidationSession:
    """Provider output is parsed structurally through the existing read-only seam."""

    def test_direct_agent_parses_unaddressed_json(self, tmp_path: Path) -> None:
        response = json.dumps({"unaddressed": [{"thread_id": "T1"}], "wont_fix": []})
        result = AgentRunResult(stdout=f"```json\n{response}\n```", stderr="")
        with (
            patch.object(review_validator, "uses_direct_agent_runner", return_value=True),
            patch.object(review_validator, "run_agent_text", return_value=result),
        ):
            unaddressed, wont_fix = review_validator._run_validation_session(
                pr_number=1,
                issue_number=2,
                worktree_path=tmp_path,
                prior_comments_json="[]",
                diff_text="diff",
                agent="codex",
                review_agent="reviewer",
                state_dir=tmp_path,
                timeout=60,
            )

        assert unaddressed == [{"thread_id": "T1"}]
        assert wont_fix == []

    def test_failed_session_returns_empty_report(self, tmp_path: Path) -> None:
        with (
            patch.object(review_validator, "uses_direct_agent_runner", return_value=True),
            patch.object(review_validator, "run_agent_text", side_effect=OSError("offline")),
        ):
            result = review_validator._run_validation_session(
                pr_number=1,
                issue_number=2,
                worktree_path=tmp_path,
                prior_comments_json="[]",
                diff_text="diff",
                agent="codex",
                review_agent="reviewer",
                state_dir=tmp_path,
                timeout=60,
            )

        assert result == ([], [])
