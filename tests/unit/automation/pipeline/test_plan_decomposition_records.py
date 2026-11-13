"""Decomposition record replay, same-epoch identity, and child readback."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from hephaestus.automation.pipeline.plan_journal import (
    DecompositionBindings,
    DecompositionIntent,
    DecompositionSeal,
    DecompositionTransitionReceipt,
    PlanRecordConflictError,
    publish_decomposition_bindings,
    publish_decomposition_intent,
    publish_decomposition_seal,
    publish_decomposition_transition,
    recover_decomposition_bindings,
    recover_decomposition_intent,
    recover_decomposition_seal,
    recover_decomposition_transition,
)
from hephaestus.automation.plan_decomposition import (
    child_recovery_marker,
    normalize_work_tree,
    normalize_work_unit,
    parse_child_marker,
    render_child_body,
    tree_digest,
)
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


def _epoch(seed: str) -> str:
    """Return one 64-hex requirements-epoch identity for tests."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _digest(seed: str) -> str:
    """Return one 64-hex identity digest for tests."""
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _intent(epoch: str) -> DecompositionIntent:
    """Return one valid decomposition intent for tests."""
    return DecompositionIntent(
        repository="acme/mono",
        parent=8,
        requirements_epoch=epoch,
        trigger="convergence",
        source_plan_revision=3,
        source_plan_fingerprint=_digest("plan"),
        requirement_manifest=({"identifier": "req-" + "0" * 12, "digest": _digest("req")},),
        finding_manifest=({"identifier": "find-" + "1" * 12, "digest": _digest("find")},),
        created_at=datetime.now(UTC).isoformat(),
    )


def _seal(epoch: str, draft_digest: str) -> DecompositionSeal:
    """Return one valid approved-tree seal for tests."""
    return DecompositionSeal(
        requirements_epoch=epoch,
        draft_digest=draft_digest,
        nodes=(
            {
                "node_id": "node-a",
                "title": "Add the parser helper",
                "owned_behavior": "Node node-a owns one parser behavior.",
                "repository_paths": ("hephaestus/utils/parser.py",),
                "acceptance_criteria": ("The parser rejects malformed payloads.",),
                "failure_behavior": "The parser raises ValueError.",
                "verification_commands": ("uv run pytest tests/unit/utils/test_parser.py",),
                "expected_evidence": "The focused parser test passes.",
                "prerequisites": (),
                "requirement_ids": ("req-" + "0" * 12,),
                "finding_ids": (),
                "single_pr": True,
            },
        ),
    )


def _transition(epoch: str) -> DecompositionTransitionReceipt:
    """Return one valid parent-transition receipt for tests."""
    return DecompositionTransitionReceipt(
        requirements_epoch=epoch,
        labels_added=("epic", "state:skip"),
        labels_removed=("state:needs-plan", "state:plan-no-go", "state:plan-go"),
        needs_plan_confirmed=("node-a",),
        summary_confirmed=True,
    )


def _unit_payload(node_id: str = "node-a", **overrides: Any) -> dict[str, Any]:
    """Return one valid closed-schema work-unit payload."""
    payload: dict[str, Any] = {
        "node_id": node_id,
        "title": "Add the parser helper",
        "owned_behavior": f"Node {node_id} owns one parser behavior.",
        "repository_paths": ("hephaestus/utils/parser.py",),
        "acceptance_criteria": ("The parser rejects malformed payloads.",),
        "failure_behavior": "The parser raises ValueError on malformed input.",
        "verification_commands": ("uv run pytest tests/unit/utils/test_parser.py",),
        "expected_evidence": "The focused parser test passes.",
        "prerequisites": (),
        "requirement_ids": ("req-" + "0" * 12,),
        "finding_ids": (),
        "single_pr": True,
    }
    payload.update(overrides)
    return payload


class TestDecompositionRecordReplay:
    """A restart replays exactly the records the run published."""

    def test_restart_recovers_exact_sealed_tree(self) -> None:
        """Every decomposition record recovers byte-exact after a restart."""
        github = FakeStageGitHub()
        epoch = _epoch("restart epoch")
        intent = _intent(epoch)
        seal = _seal(epoch, draft_digest=_digest("draft"))
        bindings = DecompositionBindings(
            requirements_epoch=epoch,
            seal_digest=seal.seal_digest,
            bindings=(("node-a", 41),),
        )
        transition = _transition(epoch)
        publish_decomposition_intent(8, intent, github)
        publish_decomposition_seal(8, seal, github)
        publish_decomposition_bindings(8, bindings, github)
        publish_decomposition_transition(8, transition, github)

        # A restart re-reads GitHub from nothing and replays the same facts.
        comments = github.issue_comments(8)
        assert recover_decomposition_intent(comments, epoch) == intent
        assert recover_decomposition_seal(comments, epoch, draft_digest=seal.draft_digest) == seal
        assert (
            recover_decomposition_bindings(comments, epoch, seal_digest=seal.seal_digest)
            == bindings
        )
        assert recover_decomposition_transition(comments, epoch) == transition

    def test_same_epoch_plan_change_cannot_replace_intent(self) -> None:
        """One epoch owns one immutable intent; a conflicting write fails closed."""
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        intent = _intent(epoch)
        seal = _seal(epoch, draft_digest=_digest("draft"))
        publish_decomposition_intent(8, intent, github)
        publish_decomposition_seal(8, seal, github)

        # A conflicting write cannot replace the durable intent or seal.
        conflicting = dataclasses.replace(intent, source_plan_fingerprint="c" * 64)
        with pytest.raises(PlanRecordConflictError, match="conflicting payload"):
            publish_decomposition_intent(8, conflicting, github)
        with pytest.raises(PlanRecordConflictError, match="conflicting payload"):
            publish_decomposition_seal(8, _seal(epoch, draft_digest="d" * 64), github)

        recovered = recover_decomposition_intent(github.issue_comments(8), epoch)
        assert recovered is not None
        assert recovered.source_plan_fingerprint == _digest("plan")
        assert recover_decomposition_seal(github.issue_comments(8), epoch) == seal

    def test_partial_records_reconcile(self) -> None:
        """A partial record set reconciles to exactly what is durable."""
        github = FakeStageGitHub()
        epoch = _epoch("partial epoch")
        seal = _seal(epoch, draft_digest=_digest("draft"))
        publish_decomposition_seal(8, seal, github)

        comments = github.issue_comments(8)
        assert recover_decomposition_seal(comments, epoch) == seal
        # No intent, bindings, or transition exists yet.
        assert recover_decomposition_intent(comments, epoch) is None
        assert recover_decomposition_bindings(comments, epoch) is None
        assert recover_decomposition_transition(comments, epoch) is None

        # Bindings bound to a different seal digest do not match the seal.
        foreign = DecompositionBindings(
            requirements_epoch=epoch,
            seal_digest="d" * 64,
            bindings=(("node-a", 41),),
        )
        publish_decomposition_bindings(8, foreign, github)
        comments = github.issue_comments(8)
        assert recover_decomposition_bindings(comments, epoch, seal_digest=seal.seal_digest) is None
        # The epoch's immutable record refuses a different replacement set.
        correct = DecompositionBindings(
            requirements_epoch=epoch,
            seal_digest=seal.seal_digest,
            bindings=(("node-a", 41),),
        )
        with pytest.raises(PlanRecordConflictError, match="conflicting payload"):
            publish_decomposition_bindings(8, correct, github)
        # The durable record is still the first one.
        assert recover_decomposition_bindings(github.issue_comments(8), epoch) == foreign


def test_child_readback_matches_complete_normalized_specification() -> None:
    """Render, marker parse, and re-render agree exactly for one child."""
    epoch = _epoch("readback epoch")
    unit = normalize_work_unit(_unit_payload())
    assert unit is not None
    marker = child_recovery_marker(
        repository="acme/mono",
        parent_issue=8,
        requirements_epoch=epoch,
        generation=1,
        node_id=unit.node_id,
    )
    body = render_child_body(
        parent_issue=8,
        unit=unit,
        prerequisite_issues={},
        plan_revision=3,
        requirements_epoch=epoch,
        marker=marker,
    )

    identity = parse_child_marker(marker)
    assert identity == {
        "repository": "acme/mono",
        "parent_issue": "8",
        "requirements_epoch": epoch,
        "generation": "1",
        "node_id": unit.node_id,
    }
    # The exact readback the runner and the coordinator both apply.
    assert body.startswith("Part of epic #8")
    for section in (
        "## Owned behavior",
        "## Repository paths",
        "## Acceptance criteria",
        "## Failure behavior",
        "## Verification commands",
        "## Expected evidence",
        "## Provenance",
    ):
        assert section in body
    assert f"- Requirements epoch: {epoch}" in body
    assert body.rstrip().endswith(marker)
    rendered_again = render_child_body(
        parent_issue=8,
        unit=unit,
        prerequisite_issues={},
        plan_revision=3,
        requirements_epoch=epoch,
        marker=marker,
    )
    assert rendered_again == body


def test_child_readback_rejects_any_specification_drift() -> None:
    """One changed specification field breaks the exact readback match."""
    epoch = _epoch("drift epoch")
    marker = child_recovery_marker(
        repository="acme/mono",
        parent_issue=8,
        requirements_epoch=epoch,
        generation=1,
        node_id="node-a",
    )

    def render(unit_payload: dict[str, Any]) -> str:
        unit = normalize_work_unit(unit_payload)
        assert unit is not None
        return render_child_body(
            parent_issue=8,
            unit=unit,
            prerequisite_issues={},
            plan_revision=3,
            requirements_epoch=epoch,
            marker=marker,
        )

    expected = render(_unit_payload())
    drifted = render(_unit_payload(owned_behavior="Node node-a owns a changed behavior."))
    assert drifted != expected


def test_sealed_tree_digest_stays_stable_across_record_replay() -> None:
    """The seal digest the coordinator binds is stable across replay."""
    epoch = _epoch("digest epoch")
    seal = _seal(epoch, draft_digest=_digest("draft"))
    units = normalize_work_tree(list(seal.nodes))
    assert units

    assert tree_digest(units) == seal.seal_digest
