"""Behavioral tests for durable staged issue-wave checkpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.issue_waves import (
    IssueWaveBlockedError,
    IssueWaveConflictError,
    IssueWaveRepositoryError,
    IssueWaveStore,
    IssueWaveValidationError,
    WaveCheckpoint,
    WaveIssueOutcome,
    WaveLease,
    WaveMergeReceipt,
    WaveRecord,
    wave_entry_from_facts,
)
from hephaestus.automation.models import DEFAULT_STATE_DIR
from hephaestus.automation.pipeline.routing import Disposition, StageName
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages.base import Continue, StageOutcome
from hephaestus.automation.pipeline.stages.repo import (
    SYNCED_MAIN_SHA_KEY,
    WAVE_ANCESTRY_VERIFIED_KEY,
    WAVE_PLAN_KEY,
    RepoStage,
)
from hephaestus.automation.pipeline.work_item import ItemKind, WorkItem
from hephaestus.automation.requirements_recovery import evidence_digest

BASE = "a" * 40
HEAD = "b" * 40
MERGE = "c" * 40
NEXT_BASE = "d" * 40
NEXT_MERGE = "e" * 40


def test_wave_selection_reopens_and_preserves_order(tmp_path: Path) -> None:
    """A second store instance resumes the exact sealed identifiers."""
    first = IssueWaveStore(tmp_path, "acme", "hephaestus")
    plan = first.plan_admission(BASE, 1)
    lease = first.seal_selection(plan, [19])

    reopened = IssueWaveStore(tmp_path, "acme", "hephaestus")
    resumed = reopened.plan_admission(BASE, 1)
    assert resumed.mode == "resume"
    assert resumed.lease == lease


def test_partial_two_issue_wave_resumes_after_first_merge_advances_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A crash after one merge retains the sealed remainder behind fresh checks."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    first = store.seal_selection(store.plan_admission(BASE, 1), [7])
    store.record_merge_receipt(
        first,
        issue_number=7,
        pr_number=17,
        reviewed_head_sha=HEAD,
        merge_sha=NEXT_BASE,
    )
    store.record_terminal_outcome(first, issue_number=7, passed=True, reason="merged", pr_number=17)
    store.verify_prior_wave(first, current_main_sha=NEXT_BASE, ancestry_verified=True)

    second = store.seal_selection(store.plan_admission(NEXT_BASE, 2), [19, 20])
    store.record_merge_receipt(
        second,
        issue_number=19,
        pr_number=29,
        reviewed_head_sha=HEAD,
        merge_sha=NEXT_MERGE,
    )

    stale_main_resume = store.plan_admission(NEXT_BASE, 2)
    assert stale_main_resume.requires_ancestry
    assert stale_main_resume.ancestor_shas == (NEXT_BASE, NEXT_MERGE)

    resumed = IssueWaveStore(tmp_path, "acme", "hephaestus").plan_admission(NEXT_MERGE, 2)

    assert resumed.mode == "resume"
    assert resumed.lease == second
    assert resumed.requires_ancestry
    assert resumed.ancestor_shas == (NEXT_BASE, NEXT_MERGE)

    merged_facts = SimpleNamespace(
        number=19,
        labels=set(),
        is_epic=False,
        pr_number=29,
        pr_is_merged=True,
        issue_is_closed=False,
    )
    facts_by_issue = {19: merged_facts}
    monkeypatch.setattr(
        "hephaestus.automation.pipeline.stages.repo._seeding.seed_issue_from_github",
        lambda number, github: facts_by_issue[number],
    )

    def resume_item() -> WorkItem:
        return WorkItem(
            repo="hephaestus",
            kind=ItemKind.REPO,
            stage=StageName.REPO,
            state="WAVE_ADMIT_AFTER_VERIFY",
            payload={
                WAVE_PLAN_KEY: resumed,
                WAVE_ANCESTRY_VERIFIED_KEY: True,
                SYNCED_MAIN_SHA_KEY: NEXT_MERGE,
            },
        )

    ctx: Any = SimpleNamespace(
        config=SimpleNamespace(issue_limit=2),
        org="acme",
        dry_run=False,
        github=object(),
        paths=SimpleNamespace(repo_root=tmp_path),
    )

    continued = RepoStage().step(resume_item(), ctx)

    assert isinstance(continued, Continue)
    assert continued.next_state == "LABELS"

    missing_proof = resume_item()
    missing_proof.payload.pop(WAVE_ANCESTRY_VERIFIED_KEY)
    result = RepoStage().step(missing_proof, ctx)

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert "ancestry verification proof is missing" in result.note

    facts_by_issue[19] = SimpleNamespace(**{**vars(merged_facts), "pr_is_merged": False})
    result = RepoStage().step(resume_item(), ctx)

    assert isinstance(result, StageOutcome)
    assert result.disposition is Disposition.FINISH_FAIL
    assert "recorded merged PR" in result.note


def test_wave_limits_progress_and_require_receipts(tmp_path: Path) -> None:
    """The next wave is blocked until a passing issue has a merge receipt."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission(BASE, 1), [19])
    store.record_terminal_outcome(
        lease, issue_number=19, passed=False, reason="agent failed", pr_number=None
    )
    with pytest.raises(IssueWaveBlockedError, match="failed"):
        store.plan_admission(BASE, 2)

    # Recovery may replace the failed terminal result only after a receipt.
    store.record_merge_receipt(
        lease,
        issue_number=19,
        pr_number=23,
        reviewed_head_sha=HEAD,
        merge_sha=MERGE,
    )
    store.record_terminal_outcome(
        lease, issue_number=19, passed=True, reason="merged", pr_number=23
    )
    next_plan = store.plan_admission(BASE, 2)
    assert next_plan.mode == "select"
    assert next_plan.requires_ancestry


def test_checkpoint_compare_and_swap_and_repository_binding(tmp_path: Path) -> None:
    """Stale plans and other repositories cannot overwrite the checkpoint."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    plan = store.plan_admission(BASE, 1)
    store.seal_selection(plan, [1])
    with pytest.raises(IssueWaveConflictError):
        store.seal_selection(plan, [2])
    with pytest.raises(IssueWaveValidationError):
        IssueWaveStore(tmp_path, "other", "hephaestus").load()


def test_empty_all_wave_and_completed_audit_are_durable(tmp_path: Path) -> None:
    """An empty final wave can complete and later runs are audit-only."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    first = store.seal_selection(store.plan_admission(BASE, 1), [])
    store.verify_prior_wave(first, current_main_sha=BASE, ancestry_verified=True)
    second_plan = store.plan_admission(BASE, 2)
    second = store.seal_selection(second_plan, [])
    # Jump through the bounded phases using empty records.
    for limit in (4, 8, None):
        store.verify_prior_wave(second, current_main_sha=BASE, ancestry_verified=True)
        second = store.seal_selection(store.plan_admission(BASE, limit), [])
    store.verify_prior_wave(second, current_main_sha=BASE, ancestry_verified=True)
    store.complete_rollout(second, current_main_sha=BASE)
    assert store.audit_only(BASE).status == "completed"
    with pytest.raises(IssueWaveBlockedError, match="already completed"):
        store.plan_admission(BASE, 1)


def test_checkpoint_rejects_symlinked_state_file(tmp_path: Path) -> None:
    """State-path confinement rejects a symlink before it can be read."""
    state = tmp_path / DEFAULT_STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    os.symlink(target, state / "issue-wave-checkpoint.json")
    with pytest.raises(IssueWaveRepositoryError):
        IssueWaveStore(tmp_path, "acme", "hephaestus").load()


def test_wave_value_objects_fail_closed_on_invalid_data() -> None:
    """The durable schema rejects malformed identities before persistence."""
    invalid_boolean: Any = 1
    with pytest.raises(IssueWaveValidationError):
        WaveLease("", "repo", 0, 1, (1,), BASE, "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveLease("org", "repo", -1, 1, (1,), BASE, "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveLease("org", "repo", 0, 3, (1,), BASE, "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveLease("org", "repo", 0, 1, (0,), BASE, "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveLease("org", "repo", 0, 1, (1,), "bad", "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveLease("org", "repo", 0, 1, (1,), BASE, "")
    with pytest.raises(IssueWaveValidationError):
        WaveMergeReceipt(0, 1, HEAD, MERGE)
    with pytest.raises(IssueWaveValidationError):
        WaveMergeReceipt(1, 0, HEAD, MERGE)
    with pytest.raises(IssueWaveValidationError):
        WaveMergeReceipt(1, 1, "bad", MERGE)
    with pytest.raises(IssueWaveValidationError):
        WaveMergeReceipt(1, 1, HEAD, "bad")
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(0, True, "done")
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(1, invalid_boolean, "done")
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(1, True, "")
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(1, True, "done", 0)
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(1, False, "failed", non_code=True)
    with pytest.raises(IssueWaveValidationError):
        WaveIssueOutcome(1, True, "done", 2, non_code=True)


def test_wave_record_and_checkpoint_invariants_are_strict() -> None:
    """Selections, receipts, generations, and completion pins cannot drift."""
    invalid_status: Any = "unknown"
    outcome = WaveIssueOutcome(1, True, "done")
    receipt = WaveMergeReceipt(1, 2, HEAD, MERGE)
    with pytest.raises(IssueWaveValidationError):
        WaveRecord(0, 1, (), BASE, "nonce", outcomes=(outcome,))
    with pytest.raises(IssueWaveValidationError):
        WaveRecord(0, 1, (1,), BASE, "nonce", outcomes=(outcome, outcome))
    with pytest.raises(IssueWaveValidationError):
        WaveRecord(0, 1, (1,), BASE, "nonce", merge_receipts=(receipt, receipt))
    orphaned_non_code = WaveIssueOutcome(1, True, "reviewed tracker", non_code=True)
    with pytest.raises(IssueWaveValidationError, match="matching active intent"):
        WaveRecord(0, 1, (1,), BASE, "nonce", outcomes=(orphaned_non_code,))
    with pytest.raises(IssueWaveValidationError):
        WaveRecord(0, 1, (1,), BASE, "nonce", verified_main_sha="bad")
    record = WaveRecord(0, 1, (1,), BASE, "nonce")
    with pytest.raises(IssueWaveValidationError):
        WaveCheckpoint("org", "repo", 0, "active", (record,))
    with pytest.raises(IssueWaveValidationError):
        WaveCheckpoint("org", "repo", 1, invalid_status, (record,))
    with pytest.raises(IssueWaveValidationError):
        WaveCheckpoint("org", "repo", 1, "active", ())
    with pytest.raises(IssueWaveValidationError):
        WaveCheckpoint("org", "repo", 1, "completed", (record,))
    with pytest.raises(IssueWaveValidationError):
        WaveCheckpoint("org", "repo", 1, "completed", (record,), "bad")


def test_wave_store_rejects_bad_selectors_and_malformed_checkpoint(tmp_path: Path) -> None:
    """Admission and on-disk decoding fail closed with actionable errors."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    assert store.plan_admission(BASE, None).mode == "ordinary"
    with pytest.raises(IssueWaveValidationError):
        store.plan_admission("bad", 1)
    with pytest.raises(IssueWaveValidationError):
        store.plan_admission(BASE, 3)
    with pytest.raises(IssueWaveBlockedError):
        store.audit_only(BASE)
    state = tmp_path / DEFAULT_STATE_DIR
    state.mkdir(parents=True, exist_ok=True)
    checkpoint = state / "issue-wave-checkpoint.json"
    checkpoint.write_text('{"schema": "wrong"}\n', encoding="utf-8")
    checkpoint.chmod(0o600)
    with pytest.raises(IssueWaveValidationError, match="schema"):
        store.load()
    checkpoint.write_text(
        '{"schema": "hephaestus.issue-wave-checkpoint.v1", "waves": {}}\n', encoding="utf-8"
    )
    with pytest.raises(IssueWaveValidationError, match="waves"):
        store.load()


def test_non_code_intent_retired_field_is_backward_compatible_and_strict(
    tmp_path: Path,
) -> None:
    """Legacy omission stays active while malformed retirement fails closed."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission(BASE, 1), [19])
    store.record_non_code_intent(
        lease,
        issue_number=19,
        reason="independently confirmed tracker",
        evidence_digest=evidence_digest("hephaestus", 19, BASE, "A task", ""),
        repository_revision=BASE,
        extra_labels=("epic",),
    )
    checkpoint_path = tmp_path / DEFAULT_STATE_DIR / "issue-wave-checkpoint.json"
    raw = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    intent = raw["waves"][0]["non_code_intents"][0]
    intent.pop("retired")
    checkpoint_path.write_text(json.dumps(raw), encoding="utf-8")

    legacy = IssueWaveStore(tmp_path, "acme", "hephaestus").non_code_intent_for(lease, 19)

    assert legacy is not None and not legacy.retired

    intent["retired"] = 1
    checkpoint_path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(IssueWaveValidationError, match="retired must be boolean"):
        IssueWaveStore(tmp_path, "acme", "hephaestus").load()

    intent["retired"] = False
    for malformed_labels in ("epic", {"epic": True}, 1, None):
        intent["extra_labels"] = malformed_labels
        checkpoint_path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(IssueWaveValidationError, match="extra_labels must be a list"):
            IssueWaveStore(tmp_path, "acme", "hephaestus").load()


def test_wave_receipts_facts_and_ancestry_are_reconciled(tmp_path: Path) -> None:
    """Fresh GitHub facts and ancestry callbacks gate durable advancement."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission(BASE, 1), [19])
    store.record_merge_receipt(
        lease, issue_number=19, pr_number=23, reviewed_head_sha=HEAD, merge_sha=MERGE
    )
    assert store.receipt_for(lease, 19) is not None
    store.record_merge_receipt(
        lease, issue_number=19, pr_number=23, reviewed_head_sha=HEAD, merge_sha=MERGE
    )
    facts = SimpleNamespace(
        number=19,
        labels=set(),
        is_epic=False,
        pr_number=23,
        pr_is_merged=True,
        issue_is_closed=False,
    )
    store.record_terminal_outcome(
        lease, issue_number=19, passed=True, reason="merged", pr_number=23
    )
    store.record_terminal_outcome(
        lease, issue_number=19, passed=True, reason="merged", pr_number=23
    )
    store.validate_prior_wave_facts(lease, {19: facts})
    verified = store.verify_prior_wave(
        lease,
        current_main_sha=BASE,
        ancestry_check=lambda current, ancestors: current == BASE and ancestors == (BASE, MERGE),
        facts_by_issue={19: facts},
    )
    assert verified.current_wave.verified_main_sha == BASE
    with pytest.raises(IssueWaveBlockedError, match="skip/block"):
        store.validate_prior_wave_facts(
            lease, {19: SimpleNamespace(**{**vars(facts), "labels": {"state:skip"}})}
        )


def test_reviewed_non_code_issue_completes_wave_without_merge_receipt(tmp_path: Path) -> None:
    """A model-confirmed tracker is durable success, not a poisoned wave."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission(BASE, 1), [19])
    reason = "independently confirmed tracker"
    store.record_non_code_intent(
        lease,
        issue_number=19,
        reason=reason,
        evidence_digest=evidence_digest("hephaestus", 19, BASE, "A task", ""),
        repository_revision=BASE,
        extra_labels=("epic",),
    )
    pending_facts = SimpleNamespace(
        number=19,
        title="A task",
        body="",
        labels=set(),
        is_epic=False,
        pr_number=None,
        pr_is_merged=False,
        issue_is_closed=False,
    )
    pending = wave_entry_from_facts(
        lease,
        pending_facts,
        SeedEntry("issue", 19, StageName.PLANNING, "pending"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert pending.stage is StageName.PLANNING
    assert pending.non_code and pending.non_code_labels == ("epic",)

    facts = SimpleNamespace(
        number=19,
        title="A task",
        body="",
        labels={"state:skip", "epic"},
        is_epic=True,
        pr_number=None,
        pr_is_merged=False,
        issue_is_closed=False,
    )
    applied = wave_entry_from_facts(
        lease,
        facts,
        SeedEntry("issue", 19, None, "state:skip"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert applied.stage is StageName.FINISHED
    assert applied.passed and applied.non_code

    sanitized = wave_entry_from_facts(
        lease,
        SimpleNamespace(**{**vars(facts), "authority_sanitized": True}),
        SeedEntry("issue", 19, None, "state:skip"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert sanitized.stage is StageName.PLANNING

    store.record_terminal_outcome(
        lease,
        issue_number=19,
        passed=True,
        reason=reason,
        non_code=True,
    )
    store.validate_prior_wave_facts(lease, {19: facts})
    verified = store.verify_prior_wave(
        lease,
        current_main_sha=BASE,
        ancestry_verified=True,
        facts_by_issue={19: facts},
    )
    assert verified.current_wave.verified_main_sha == BASE
    assert store.plan_admission(BASE, 2).mode == "select"

    resumed = wave_entry_from_facts(
        lease,
        facts,
        SeedEntry("issue", 19, None, "state:skip"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert resumed.stage is StageName.FINISHED
    assert resumed.passed and resumed.non_code and resumed.reason == reason

    missing_epic = wave_entry_from_facts(
        lease,
        SimpleNamespace(**{**vars(facts), "labels": {"state:skip"}}),
        SeedEntry("issue", 19, None, "state:skip"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert missing_epic.stage is StageName.FINISHED
    assert missing_epic.passed is False
    assert "lost its reviewed non-code skip" in missing_epic.reason

    with pytest.raises(IssueWaveBlockedError, match="non-code skip"):
        store.validate_prior_wave_facts(
            lease,
            {19: SimpleNamespace(**{**vars(facts), "labels": {"epic"}})},
        )


def test_retired_non_code_intent_reopens_only_for_cleanup(tmp_path: Path) -> None:
    """A revoked intent survives restart as cleanup provenance, never skip authority."""
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    lease = store.seal_selection(store.plan_admission(BASE, 1), [21])
    store.record_non_code_intent(
        lease,
        issue_number=21,
        reason="independently confirmed tracker",
        evidence_digest=evidence_digest("hephaestus", 21, BASE, "A task", "Old body"),
        repository_revision=BASE,
        extra_labels=("epic",),
    )
    active = store.non_code_intent_for(lease, 21)
    assert active is not None

    store.retire_non_code_intent(lease, active)
    store.retire_non_code_intent(lease, active)

    reopened = IssueWaveStore(tmp_path, "acme", "hephaestus")
    retired = reopened.non_code_intent_for(lease, 21)
    assert retired is not None and retired.retired
    facts = SimpleNamespace(
        number=21,
        title="A task",
        body="Implement the worker.",
        labels={"state:skip", "epic"},
        is_epic=True,
        pr_number=None,
        pr_is_merged=False,
        issue_is_closed=False,
    )

    entry = wave_entry_from_facts(
        lease,
        facts,
        SeedEntry("issue", 21, None, "state:skip"),
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )

    assert entry.stage is StageName.PLANNING
    assert entry.non_code and entry.non_code_retired
    reopened.complete_non_code_intent_retirement(lease, retired)
    reopened.complete_non_code_intent_retirement(lease, retired)
    assert reopened.non_code_intent_for(lease, 21) is None


def test_wave_entry_reconciles_post_seal_drift_without_mutation(tmp_path: Path) -> None:
    """A sealed issue becomes terminally failed unless its receipt proves merge."""
    entry = SeedEntry("issue", 19, StageName.PLANNING, "pending")
    outside = wave_entry_from_facts(
        WaveLease("acme", "hephaestus", 0, 1, (19,), BASE, "nonce"),
        SimpleNamespace(number=20),
        entry,
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert outside.stage is StageName.FINISHED and not outside.passed
    lease = IssueWaveStore(tmp_path, "acme", "hephaestus").seal_selection(
        IssueWaveStore(tmp_path, "acme", "hephaestus").plan_admission(BASE, 1), [19]
    )
    store = IssueWaveStore(tmp_path, "acme", "hephaestus")
    store.record_merge_receipt(
        lease, issue_number=19, pr_number=23, reviewed_head_sha=HEAD, merge_sha=MERGE
    )
    merged = wave_entry_from_facts(
        lease,
        SimpleNamespace(
            number=19, pr_is_merged=True, pr_number=23, issue_is_closed=False, is_epic=False
        ),
        entry,
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert merged.stage is StageName.FINISHED and merged.passed
    pending = wave_entry_from_facts(
        lease,
        SimpleNamespace(
            number=19, pr_is_merged=False, pr_number=None, issue_is_closed=False, is_epic=False
        ),
        entry,
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert pending.stage is StageName.PLANNING
    tracker = wave_entry_from_facts(
        lease,
        SimpleNamespace(
            number=19, pr_is_merged=False, pr_number=None, issue_is_closed=False, is_epic=True
        ),
        entry,
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert tracker.stage is StageName.PLANNING
    closed = wave_entry_from_facts(
        lease,
        SimpleNamespace(
            number=19, pr_is_merged=False, pr_number=None, issue_is_closed=True, is_epic=False
        ),
        entry,
        repo_root=tmp_path,
        org="acme",
        repo="hephaestus",
    )
    assert closed.stage is StageName.FINISHED and not closed.passed
