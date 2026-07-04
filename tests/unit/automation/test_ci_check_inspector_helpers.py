"""Unit tests for CICheckInspector collaborator (refs #1179)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.ci_check_inspector import (
    FAILING_CHECK_CONCLUSIONS,
    CICheckInspector,
)


@pytest.fixture()
def inspector() -> CICheckInspector:
    """Return a CICheckInspector wired with test doubles."""
    return CICheckInspector(
        get_pr_branch=lambda pr: f"branch-{pr}",
        options_provider=lambda: MagicMock(dry_run=False),
    )


class TestFailingCheckConclusions:
    """Tests for the FAILING_CHECK_CONCLUSIONS constant."""

    def test_contains_expected_values(self) -> None:
        assert "FAILURE" in FAILING_CHECK_CONCLUSIONS
        assert "CANCELLED" in FAILING_CHECK_CONCLUSIONS
        assert "TIMED_OUT" in FAILING_CHECK_CONCLUSIONS

    def test_is_frozenset(self) -> None:
        assert isinstance(FAILING_CHECK_CONCLUSIONS, frozenset)

    def test_success_not_included(self) -> None:
        assert "SUCCESS" not in FAILING_CHECK_CONCLUSIONS
        assert "PENDING" not in FAILING_CHECK_CONCLUSIONS


class TestFailingRequiredCheckNames:
    """Tests for CICheckInspector.failing_required_check_names."""

    def test_returns_required_failing_check_names(self, inspector: CICheckInspector) -> None:
        # gh_pr_checks returns dicts with lowercase "conclusion" and "required" key
        checks = [
            {"name": "lint", "conclusion": "failure", "status": "completed", "required": True},
            {"name": "tests", "conclusion": "success", "status": "completed", "required": True},
            {"name": "optional", "conclusion": "failure", "status": "completed", "required": False},
        ]
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_checks",
            return_value=checks,
        ):
            result = inspector.failing_required_check_names(42)
        assert result == ["lint"]

    def test_empty_when_no_required_checks_fail(self, inspector: CICheckInspector) -> None:
        checks = [
            {"name": "lint", "conclusion": "success", "status": "completed", "required": True},
        ]
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_checks",
            return_value=checks,
        ):
            result = inspector.failing_required_check_names(42)
        assert result == []


class TestPendingRequiredCheckNames:
    """Tests for CICheckInspector.pending_required_check_names."""

    def test_returns_pending_required_checks(self, inspector: CICheckInspector) -> None:
        checks = [
            {"name": "slow-ci", "conclusion": None, "status": "in_progress", "required": True},
            {"name": "fast-ci", "conclusion": "success", "status": "completed", "required": True},
        ]
        with patch(
            "hephaestus.automation.ci_check_inspector.gh_pr_checks",
            return_value=checks,
        ):
            result = inspector.pending_required_check_names(42)
        assert "slow-ci" in result


class TestErrorExcerpt:
    """Tests for the _error_excerpt failed-log excerpting helper."""

    def test_buried_error_is_surfaced_over_head_banners(self) -> None:
        """Setup banners must not displace the real error (#1780 shakedown)."""
        from hephaestus.automation.ci_check_inspector import _error_excerpt

        banner = 'echo "✅ CI build validation succeeded"\n' + ("setup line\n" * 500)
        log = banner + "train.mojo:149:20: error: value cannot be implicitly copied\nmore\n"
        excerpt = _error_excerpt(log, limit=3000)
        assert "error: value cannot be implicitly copied" in excerpt

    def test_no_error_lines_falls_back_to_tail(self) -> None:
        from hephaestus.automation.ci_check_inspector import _error_excerpt

        log = "\n".join(f"line {i}" for i in range(1000))
        excerpt = _error_excerpt(log, limit=100)
        assert excerpt == log[-100:]

    def test_error_context_lines_included(self) -> None:
        from hephaestus.automation.ci_check_inspector import _error_excerpt

        log = "before2\nbefore1\nBuild FAILED with exit 1\nafter1\nafter2\n"
        excerpt = _error_excerpt(log)
        assert "before2" in excerpt
        assert "after2" in excerpt

    def test_overflow_keeps_the_tail_errors(self) -> None:
        from hephaestus.automation.ci_check_inspector import _error_excerpt

        log = "\n".join(f"error: number {i}" for i in range(1000))
        excerpt = _error_excerpt(log, limit=200)
        assert "error: number 999" in excerpt
        assert len(excerpt) <= 200
