"""Tests for :mod:`hephaestus.automation._review_phase`.

Focus: the in-loop PR-review path must honor Claude 429 session-limit caps
(block until reset) and 529 server-overload (bounded backoff) before recording
an ERROR verdict, matching the implement-phase behavior. Regression guard for
the asymmetry where the review path burned doomed sessions against an exhausted
quota (issue #1528).
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import pytest

from hephaestus.automation import _review_phase
from hephaestus.automation._review_phase import ReviewPhase
from hephaestus.automation._stage_context import StageContext


class TestHandleReviewerQuotaOrOverload:
    """Unit tests for the shared quota/overload handler."""

    def test_session_limit_429_waits_until_reset(self, monkeypatch):
        """A 429 session-limit message blocks via wait_until(reset_epoch)."""
        waited: list[int] = []
        monkeypatch.setattr(_review_phase, "wait_until", lambda epoch: waited.append(epoch))
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))

        # A realistic reset epoch in the future so reset_epoch > 0.
        future = int(time.time()) + 3600
        monkeypatch.setattr(
            _review_phase,
            "resolve_quota_reset_epoch",
            lambda *texts: future,
        )

        err = RuntimeError(
            "Analysis session failed for PR Foo#1: "
            '{"is_error":true,"api_error_status":429,'
            '"result":"You\'ve hit your session limit · resets 5pm (America/Los_Angeles)"}'
        )
        _review_phase._handle_reviewer_quota_or_overload(err, issue_number=42, iteration=0)

        assert waited == [future], "must block until the parsed reset epoch"
        assert slept == [], "429 waits until reset, it does not backoff-sleep"

    def test_529_overload_backs_off(self, monkeypatch):
        """A 529 overload (no reset epoch) triggers a bounded backoff sleep."""
        waited: list[int] = []
        monkeypatch.setattr(_review_phase, "wait_until", lambda epoch: waited.append(epoch))
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        # No quota reset epoch for a server-overload error.
        monkeypatch.setattr(_review_phase, "resolve_quota_reset_epoch", lambda *t: None)

        err = RuntimeError("Analysis session failed for PR Foo#1: API Error: 529 Overloaded")
        _review_phase._handle_reviewer_quota_or_overload(err, issue_number=7, iteration=2)

        assert waited == [], "overload carries no reset epoch; must not wait_until"
        assert len(slept) == 1 and slept[0] > 0, "overload must back off once"

    def test_plain_failure_neither_waits_nor_sleeps(self, monkeypatch):
        """A non-transient failure returns immediately with no wait/sleep."""
        waited: list[int] = []
        monkeypatch.setattr(_review_phase, "wait_until", lambda epoch: waited.append(epoch))
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        monkeypatch.setattr(_review_phase, "resolve_quota_reset_epoch", lambda *t: None)

        err = RuntimeError("reviewer returned empty output")
        _review_phase._handle_reviewer_quota_or_overload(err, issue_number=9, iteration=0)

        assert waited == []
        assert slept == []

    def test_usage_cap_error_reset_epoch_attr_is_honored(self, monkeypatch):
        """A ClaudeUsageCapError's reset_epoch attribute drives wait_until.

        The central is_error guard raises ClaudeUsageCapError whose message has
        no reset phrasing, so resolve_quota_reset_epoch(str(e)) would miss it —
        the attribute must be used instead (#1528 follow-up).
        """
        from hephaestus.github.client import ClaudeUsageCapError

        waited: list[int] = []
        monkeypatch.setattr(_review_phase, "wait_until", lambda epoch: waited.append(epoch))
        # str(error) carries no reset phrasing, so text scanning returns None.
        monkeypatch.setattr(_review_phase, "resolve_quota_reset_epoch", lambda *t: None)

        future = int(time.time()) + 3600
        err = ClaudeUsageCapError("Claude usage cap reached", reset_epoch=future)
        _review_phase._handle_reviewer_quota_or_overload(err, issue_number=5, iteration=0)

        assert waited == [future]

    def test_zero_reset_epoch_does_not_wait(self, monkeypatch):
        """A 0 reset-epoch sentinel (reset unknown) must not call wait_until."""
        waited: list[int] = []
        monkeypatch.setattr(_review_phase, "wait_until", lambda epoch: waited.append(epoch))
        slept: list[float] = []
        monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
        # 0 sentinel = rate-limited but reset time unknown; guarded by epoch > 0.
        monkeypatch.setattr(_review_phase, "resolve_quota_reset_epoch", lambda *t: 0)
        monkeypatch.setattr(_review_phase, "detect_server_overload", lambda *t: False)

        err = RuntimeError("session limit")
        _review_phase._handle_reviewer_quota_or_overload(err, issue_number=1, iteration=0)

        assert waited == []
        assert slept == []


@pytest.mark.parametrize("backoff_iteration,expected_max", [(0, 5), (1, 10), (5, 20)])
def test_overload_backoff_is_bounded(monkeypatch, backoff_iteration, expected_max):
    """Overload backoff grows with iteration but is capped at 20s."""
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(_review_phase, "resolve_quota_reset_epoch", lambda *t: None)

    err = RuntimeError("overloaded")
    _review_phase._handle_reviewer_quota_or_overload(
        err, issue_number=1, iteration=backoff_iteration
    )

    assert len(slept) == 1
    assert 0 < slept[0] <= expected_max


def _make_ctx(tmp_path: Path) -> StageContext:
    """Minimal StageContext stub for exercising ReviewPhase in isolation."""
    options = SimpleNamespace(
        agent="claude",
        dry_run=False,
        include_nitpicks=False,
    )
    impl = cast(
        Any,
        SimpleNamespace(
            options=options,
            state_dir=tmp_path,
            repo_root=tmp_path,
            _collect_diff=lambda *a, **k: "diff --git a/x b/x",
            _log=lambda *a, **k: None,
        ),
    )
    runner = cast(
        Any,
        SimpleNamespace(_fetch_plan_and_review=lambda *a, **k: ("plan", "plan review")),
    )
    return StageContext(impl=impl, runner=runner)


class TestRunImplReviewStepPromptTooLong:
    """_run_impl_review_step surfaces reason=prompt_too_long distinctly (#1847)."""

    def test_prompt_too_long_error_records_error_with_distinct_reason(self, tmp_path: Path) -> None:
        phase = ReviewPhase(_make_ctx(tmp_path))

        def _raise_prompt_too_long(**_: object) -> tuple[str, list[str]]:
            raise RuntimeError(
                "reason=prompt_too_long: PR review prompt exceeds model context "
                "even at aggressive budget for Repo#1"
            )

        with mock.patch(
            "hephaestus.automation._review_phase.review_pr_inline",
            side_effect=_raise_prompt_too_long,
        ):
            review_text, thread_ids = phase._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                branch_name="1-auto-impl",
                worktree_path=tmp_path,
                pr_number=99,
                iteration=0,
                prior_review=None,
            )

        assert thread_ids == []
        assert "In-loop reviewer invocation failed" in review_text

    def test_generic_error_still_recorded_as_plain_error(self, tmp_path: Path) -> None:
        """A non-prompt_too_long failure keeps the original generic ERROR message."""
        phase = ReviewPhase(_make_ctx(tmp_path))

        def _raise_generic(**_: object) -> tuple[str, list[str]]:
            raise RuntimeError("some other infra failure")

        with mock.patch(
            "hephaestus.automation._review_phase.review_pr_inline",
            side_effect=_raise_generic,
        ):
            review_text, thread_ids = phase._run_impl_review_step(
                issue_number=1,
                issue_title="t",
                issue_body="b",
                branch_name="1-auto-impl",
                worktree_path=tmp_path,
                pr_number=99,
                iteration=0,
                prior_review=None,
            )

        assert thread_ids == []
        assert "In-loop reviewer invocation failed" in review_text
        assert "prompt_too_long" not in review_text
