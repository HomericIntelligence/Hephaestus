"""Tests for durable plan-review conversation state."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.agents.execution_policy import AgentRole
from hephaestus.agents.pi_session import create_pi_binding
from hephaestus.automation.plan_review_session import (
    PlanReviewSessionLostError,
    PlanReviewSessionStore,
)


def _store(tmp_path: Path) -> PlanReviewSessionStore:
    return PlanReviewSessionStore(lambda: tmp_path)


def test_cycle_recovery_preserves_session_and_complete_transcript(tmp_path: Path) -> None:
    """A restarted runner resumes the same cycle and complete review history."""
    store = _store(tmp_path)
    record = store.start_cycle(
        repo="org/repo",
        issue=639,
        provider="codex",
        model="reviewer",
        reviewer_config={"reasoning_effort": "medium"},
        cwd=tmp_path / "worktree",
        plan_revision=1,
        plan_fingerprint="plan-v1",
    )
    store.bind_session(record.cycle_id, "opaque-provider-session")
    store.append_artifact(
        record.cycle_id,
        kind="review",
        content="finding from round zero",
        round_index=0,
        plan_revision=1,
        plan_fingerprint="plan-v1",
    )
    store.append_artifact(
        record.cycle_id,
        kind="review",
        content="finding from round zero",
        round_index=0,
        plan_revision=1,
        plan_fingerprint="plan-v1",
    )
    store.append_artifact(
        record.cycle_id,
        kind="amendment",
        content="revised plan",
        round_index=0,
        plan_revision=2,
        plan_fingerprint="plan-v2",
    )

    restarted = _store(tmp_path).recover_active(repo="org/repo", issue=639)

    assert restarted is not None
    assert restarted.cycle_id == record.cycle_id
    assert restarted.session_id == "opaque-provider-session"
    assert restarted.plan_revision == 2
    assert restarted.plan_fingerprint == "plan-v2"
    assert [artifact.kind for artifact in restarted.artifacts] == ["review", "amendment"]
    assert "finding from round zero" in store.transcript(restarted.cycle_id)
    assert "revised plan" in store.transcript(restarted.cycle_id)


def test_cycle_recovery_preserves_provider_binding(tmp_path: Path) -> None:
    """Provider-specific resume state survives a process restart."""
    store = _store(tmp_path)
    record = store.start_cycle(
        repo="org/repo",
        issue=2766,
        provider="pi",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="plan-v1",
    )
    binding = create_pi_binding(
        session_id="opaque-pi-session",
        cwd=tmp_path,
        role=AgentRole.PLAN_REVIEWER,
        model="reviewer",
        state_reference="private-state-ref",
    )

    store.bind_session(record.cycle_id, binding.session_id, binding)

    restarted = _store(tmp_path).recover_active(repo="org/repo", issue=2766)
    assert restarted is not None
    assert restarted.session_binding == binding.to_json()


def test_separate_issues_cycles_and_reset_do_not_share_state(tmp_path: Path) -> None:
    """Issue/cycle isolation and explicit reset always create a new identity."""
    store = _store(tmp_path)
    first = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="claude",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
    )
    other = store.start_cycle(
        repo="org/repo",
        issue=2,
        provider="claude",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="b",
    )
    reset = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="claude",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
        reset=True,
    )

    assert len({first.cycle_id, other.cycle_id, reset.cycle_id}) == 3
    assert store.recover_active(repo="org/repo", issue=1) == reset


def test_missing_or_tampered_resume_state_fails_closed(tmp_path: Path) -> None:
    """Durable evidence loss never falls back to a fresh reviewer."""
    store = _store(tmp_path)
    record = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="codex",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
    )
    store.bind_session(record.cycle_id, "opaque")
    store.append_artifact(
        record.cycle_id,
        kind="review",
        content="review",
        round_index=0,
        plan_revision=1,
        plan_fingerprint="a",
    )
    artifact_path = store.artifact_path(record.cycle_id, 0)
    artifact_path.write_text("tampered", encoding="utf-8")

    with pytest.raises(PlanReviewSessionLostError, match="artifact"):
        store.transcript(record.cycle_id)

    active_path = store.active_path("org/repo", 1)
    active_path.write_text(json.dumps({"cycle_id": "missing"}), encoding="utf-8")
    with pytest.raises(PlanReviewSessionLostError, match="missing"):
        store.recover_active(repo="org/repo", issue=1)


def test_malformed_cycle_record_fails_closed(tmp_path: Path) -> None:
    """A syntactically valid but invalid journal cannot be resumed."""
    store = _store(tmp_path)
    record = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="codex",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
    )
    path = store.record_path(record.cycle_id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["state"] = "silently-start-fresh"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PlanReviewSessionLostError, match="state"):
        store.recover_active(repo="org/repo", issue=1)


def test_explicit_reset_recovers_from_recovery_required_state(tmp_path: Path) -> None:
    """Only an explicit reset may replace a terminally lost conversation."""
    store = _store(tmp_path)
    lost = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="codex",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
    )
    store.mark_recovery_required(lost.cycle_id)
    with pytest.raises(PlanReviewSessionLostError, match="recovery-required"):
        store.recover_active(repo="org/repo", issue=1)

    replacement = store.start_cycle(
        repo="org/repo",
        issue=1,
        provider="codex",
        model="reviewer",
        reviewer_config={},
        cwd=tmp_path,
        plan_revision=1,
        plan_fingerprint="a",
        reset=True,
    )
    assert replacement.cycle_id != lost.cycle_id
    assert store.recover_active(repo="org/repo", issue=1) == replacement
