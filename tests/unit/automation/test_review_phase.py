"""Tests for :mod:`hephaestus.automation._review_phase`.

Focus: the in-loop PR-review path must honor Claude 429 session-limit caps
(block until reset) and 529 server-overload (bounded backoff) before recording
an ERROR verdict, matching the implement-phase behavior. Regression guard for
the asymmetry where the review path burned doomed sessions against an exhausted
quota (issue #1528).
"""

from __future__ import annotations

import time

import pytest

from hephaestus.automation import _review_phase


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
