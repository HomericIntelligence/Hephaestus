"""Crash-recovery and no-progress tests for the durable plan journal."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.plan_journal import (
    DecompositionBindings,
    DecompositionIntent,
    DecompositionSeal,
    DecompositionTransitionReceipt,
    PlanNogoRecord,
    PlanRecordConflictError,
    plan_nogo_count,
    plan_nogo_digest,
    plan_nogo_records,
    publish_decomposition_bindings,
    publish_decomposition_draft,
    publish_decomposition_intent,
    publish_decomposition_seal,
    publish_decomposition_transition,
    publish_plan_nogo,
    publish_plan_revision,
    reconcile_plan_journal,
    recover_decomposition_bindings,
    recover_decomposition_draft,
    recover_decomposition_intent,
    recover_decomposition_seal,
    recover_decomposition_transition,
    render_decomposition_summary_body,
    requirements_epoch,
)
from hephaestus.automation.protocol import PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    IssueComment,
    journal_snapshot,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import STATE_PLAN_NO_GO
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


class _CrashOnceJournalGitHub(FakeStageGitHub):
    """Inject one crash at a selected write in a plan-revision transaction."""

    def __init__(self, crash_on: str) -> None:
        super().__init__(labels=[STATE_PLAN_NO_GO])
        self.crash_on = crash_on
        self.crashed = False

    def _crash(self, target: str) -> None:
        if not self.crashed and self.crash_on == target:
            self.crashed = True
            raise RuntimeError(f"injected {target} crash")

    def upsert_plan_comment(self, issue_number: int, body: str) -> None:
        if "<!-- revision: 2 -->" in body:
            self._crash("canonical_plan")
        super().upsert_plan_comment(issue_number, body)

    def upsert_issue_comment(
        self,
        issue_number: int,
        marker: str,
        body: str,
        *,
        legacy_marker: str | None = None,
    ) -> None:
        if marker == PLAN_REVIEW_CANONICAL_MARKER and "revision 2" in body:
            self._crash("pending_review")
        super().upsert_issue_comment(
            issue_number,
            marker,
            body,
            legacy_marker=legacy_marker,
        )


def test_restart_repairs_a_plan_written_before_pending_review() -> None:
    """A restart replaces a stale review after the revised plan became durable."""
    github = _CrashOnceJournalGitHub("pending_review")
    github.comments[7] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]

    with pytest.raises(RuntimeError, match="pending_review"):
        publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    reconcile_plan_journal(7, github)
    snapshot = journal_snapshot(github.issue_comments(7))

    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2 with rollback"
    assert snapshot.current_review_revision == 2
    mutations_after_recovery = list(github.mutation_log)

    reconcile_plan_journal(7, github)

    assert github.mutation_log == mutations_after_recovery


def test_forced_publication_persists_restart_provenance() -> None:
    """The canonical plan records that its pending review belongs to force."""
    github = FakeStageGitHub()

    publish_plan_revision(
        10,
        "Fresh forced plan",
        github,
        require_change=False,
        forced_planning_epoch=True,
    )

    snapshot = journal_snapshot(github.issue_comments(10))
    assert snapshot.current_plan == "Fresh forced plan"
    assert snapshot.forced_planning_epoch is True


def test_epoch_marker_examples_in_plan_prose_are_not_metadata() -> None:
    """Only fixed host header positions carry restart authority."""
    payload = (
        "Keep these examples literal:\n\n"
        "<!-- hephaestus-forced-planning-epoch -->\n\n"
        "```markdown\n"
        "<!-- hephaestus-forced-planning-epoch -->\n"
        "```"
    )

    rendered = render_current_plan(payload, revision=3)
    snapshot = journal_snapshot([IssueComment(body=rendered, viewer_did_author=True)])

    assert snapshot.current_plan == payload
    assert snapshot.forced_planning_epoch is False


def test_failed_canonical_plan_write_preserves_the_previous_revision_for_retry() -> None:
    """A failed first write loses no public artifact and a clean retry can publish."""
    github = _CrashOnceJournalGitHub("canonical_plan")
    github.comments[7] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]

    with pytest.raises(RuntimeError, match="canonical_plan"):
        publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    preserved = journal_snapshot(github.issue_comments(7))
    assert preserved.revision == 1
    assert preserved.current_plan == "Plan v1"
    assert preserved.current_review == "Needs rollback."

    published = publish_plan_revision(7, "Plan v2 with rollback", github, require_change=True)

    assert published.revision == 2
    assert len(github.comments[7]) == 2


def test_plan_oscillation_is_blocked_before_v1_is_republished() -> None:
    """A v1 -> v2 -> v1 cycle is detected from immutable journal history."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[8] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.", revision=1),
    ]
    publish_plan_revision(8, "Plan v2", github, require_change=True)
    before = list(github.comments[8])

    result = publish_plan_revision(8, "Plan v1", github, require_change=True)

    assert result.is_stuck
    assert "repeats a previous plan" in result.no_progress_reason
    assert github.comments[8] == before


@pytest.mark.parametrize(
    "candidate",
    [
        "# Implementation Plan\n\n```diff\n-old\n+new\n```",
        "# Implementation Plan\n\ndiff --git a/a.py b/a.py\n@@ -1 +1 @@",
    ],
)
def test_publication_rejects_raw_patch_content(candidate: str) -> None:
    """A generated patch can never become a public canonical plan comment."""
    github = FakeStageGitHub()

    result = publish_plan_revision(8, candidate, github, require_change=False)

    assert result.is_stuck
    assert "raw patch" in result.no_progress_reason
    assert github.comments.get(8, []) == []


def test_amendment_replaces_canonical_comments_without_public_history() -> None:
    """A revised plan leaves only the latest plan and latest review on the issue."""
    github = FakeStageGitHub(labels=[STATE_PLAN_NO_GO])
    github.comments[8] = [
        render_current_plan("Plan v1", revision=1),
        render_current_review("Needs rollback.\n\nstate:plan-no-go", revision=1),
    ]

    result = publish_plan_revision(8, "Plan v2 with rollback", github, require_change=True)

    assert result.changed
    assert len(github.comments[8]) == 2
    snapshot = journal_snapshot(github.issue_comments(8))
    assert snapshot.revision == 2
    assert snapshot.current_plan == "Plan v2 with rollback"
    assert snapshot.current_review_revision == 2
    public_text = "\n".join(str(comment) for comment in github.comments[8])
    assert "Previous Implementation Plan" not in public_text
    assert "Review of Previous Plan" not in public_text
    assert "```diff" not in public_text


def test_publication_rejects_concurrent_canonical_pointer_overwrite() -> None:
    """A writer cannot report success after another writer replaces its plan."""

    class CompetingWriterGitHub(FakeStageGitHub):
        def upsert_plan_comment(self, issue_number: int, body: str) -> None:
            super().upsert_plan_comment(issue_number, body)
            super().upsert_plan_comment(
                issue_number,
                render_current_plan("Competing plan", revision=1),
            )

    github = CompetingWriterGitHub()

    with pytest.raises(RuntimeError, match=r"concurrent plan journal write.*another pipeline item"):
        publish_plan_revision(9, "Expected plan", github, require_change=False)


def test_publication_rejects_normalized_equivalent_canonical_overwrite() -> None:
    """Confirmation rejects a byte-different plan with the same fingerprint."""

    class CompetingWriterGitHub(FakeStageGitHub):
        def upsert_plan_comment(self, issue_number: int, body: str) -> None:
            super().upsert_plan_comment(issue_number, body)
            super().upsert_plan_comment(issue_number, body.replace("\n", "\r\n"))

    github = CompetingWriterGitHub()

    with pytest.raises(RuntimeError, match=r"concurrent plan journal write.*another pipeline item"):
        publish_plan_revision(10, "Expected plan", github, require_change=False)


def _epoch(seed: str) -> str:
    """Return one 64-hex requirements-epoch identity for tests."""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _digest(seed: str) -> str:
    """Return one 64-hex identity digest for tests."""
    import hashlib

    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _nogo(
    seed: str = "round-1",
    epoch: str = "a" * 64,
    cycle: int = 0,
    round_value: int = 1,
    revision: int = 1,
) -> PlanNogoRecord:
    """Return one valid NOGO record for tests."""
    from datetime import UTC, datetime

    return PlanNogoRecord(
        repository="acme/mono",
        issue=8,
        requirements_epoch=epoch,
        plan_revision=revision,
        review_cycle=cycle,
        review_round=round_value,
        review_digest=_digest(seed),
        findings=("Guard the empty payload case.",),
        recorded_at=datetime.now(UTC).isoformat(),
    )


def _intent(epoch: str = "a" * 64, fingerprint: str = "b" * 64) -> DecompositionIntent:
    """Return one valid decomposition intent for tests."""
    from datetime import UTC, datetime

    return DecompositionIntent(
        repository="acme/mono",
        parent=8,
        requirements_epoch=epoch,
        trigger="convergence",
        source_plan_revision=3,
        source_plan_fingerprint=fingerprint,
        requirement_manifest=({"identifier": "req-" + "0" * 12, "digest": _digest("req")},),
        finding_manifest=({"identifier": "find-" + "1" * 12, "digest": _digest("find")},),
        created_at=datetime.now(UTC).isoformat(),
    )


def _seal(epoch: str = "a" * 64, draft_digest: str = "c" * 64) -> DecompositionSeal:
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


class TestPlanNogoRecords:
    """Durable NOGO records count real verdicts per requirements epoch."""

    def test_publish_is_idempotent_on_the_same_review_identity(self) -> None:
        github = FakeStageGitHub()
        record = _nogo()

        publish_plan_nogo(8, record, github)
        publish_plan_nogo(8, record, github)

        assert len(github.comments[8]) == 1
        records = plan_nogo_records(github.issue_comments(8))
        assert len(records) == 1
        assert records[0].review_digest == record.review_digest

    def test_conflicting_payload_for_the_same_identity_fails_closed(self) -> None:
        github = FakeStageGitHub()
        record = _nogo()

        publish_plan_nogo(8, record, github)
        conflicting = PlanNogoRecord(
            repository=record.repository,
            issue=record.issue,
            requirements_epoch=record.requirements_epoch,
            plan_revision=record.plan_revision,
            review_cycle=record.review_cycle,
            review_round=record.review_round,
            review_digest=record.review_digest,
            findings=(),
            recorded_at=record.recorded_at,
        )

        with pytest.raises(PlanRecordConflictError, match="conflicting payload"):
            publish_plan_nogo(8, conflicting, github)

    def test_malformed_and_foreign_records_are_ignored_by_the_count(self) -> None:
        github = FakeStageGitHub()
        publish_plan_nogo(8, _nogo(), github)
        github.comments[8].append("<!-- hephaestus-plan-nogo:v1:" + ("e" * 64) + " -->\nnot json")
        github.comments[8].append("A foreign comment.")

        records = plan_nogo_records(github.issue_comments(8))

        assert len(records) == 1
        assert plan_nogo_count(github.issue_comments(8), "a" * 64) == 1

    def test_issue_body_change_resets_nogo_epoch(self) -> None:
        """Records for the old epoch stop counting after the issue body changes."""
        github = FakeStageGitHub()
        old_epoch = _epoch("old issue body")
        new_epoch = _epoch("new issue body")

        publish_plan_nogo(8, _nogo(seed="r1", epoch=old_epoch), github)
        publish_plan_nogo(8, _nogo(seed="r2", epoch=old_epoch, round_value=2), github)

        assert plan_nogo_count(github.issue_comments(8), old_epoch) == 2
        assert plan_nogo_count(github.issue_comments(8), new_epoch) == 0

    def test_counts_survive_plan_cycle_resets_and_restarts(self) -> None:
        """The journal counts across plan cycles; only the epoch filters."""
        github = FakeStageGitHub()
        epoch = _epoch("stable requirements")

        for index in range(1, 6):
            publish_plan_nogo(
                8,
                _nogo(
                    seed=f"cycle-{index}",
                    epoch=epoch,
                    cycle=0 if index <= 3 else 1,
                    round_value=index if index <= 3 else index - 3,
                ),
                github,
            )

        assert plan_nogo_count(github.issue_comments(8), epoch) == 5

    def test_review_digest_binds_the_review_identity(self) -> None:
        first = plan_nogo_digest(
            plan_revision=1, review_cycle=0, review_round=1, review_raw="NOGO text"
        )
        same = plan_nogo_digest(
            plan_revision=1, review_cycle=0, review_round=1, review_raw="NOGO text"
        )
        other = plan_nogo_digest(
            plan_revision=1, review_cycle=0, review_round=2, review_raw="NOGO text"
        )
        assert first == same
        assert first != other

    def test_requirements_epoch_uses_the_authoritative_issue_body(self) -> None:
        assert requirements_epoch("The requirements.") == _epoch("The requirements.")
        assert requirements_epoch("A") != requirements_epoch("B")


class TestDecompositionRecords:
    """Intent, seal, bindings, draft, and transition records reconcile exactly."""

    def test_same_epoch_plan_change_cannot_replace_intent(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")

        publish_decomposition_intent(8, _intent(epoch=epoch), github)
        with pytest.raises(PlanRecordConflictError, match="conflicting payload"):
            publish_decomposition_intent(8, _intent(epoch=epoch, fingerprint="c" * 64), github)

        recovered = recover_decomposition_intent(github.issue_comments(8), epoch)
        assert recovered is not None
        assert recovered.source_plan_fingerprint == "b" * 64

    def test_intent_recovers_across_a_restart(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        intent = _intent(epoch=epoch)
        publish_decomposition_intent(8, intent, github)

        recovered = recover_decomposition_intent(github.issue_comments(8), epoch)

        assert recovered == intent

    def test_restart_recovers_exact_sealed_tree(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        seal = _seal(epoch=epoch)
        publish_decomposition_seal(8, seal, github)

        recovered = recover_decomposition_seal(
            github.issue_comments(8), epoch, draft_digest=seal.draft_digest
        )

        assert recovered == seal
        assert recovered is not None
        assert recovered.seal_digest == seal.seal_digest

    def test_seal_bound_to_another_draft_is_not_recovered(self) -> None:
        github = FakeStageGitHub()
        publish_decomposition_seal(8, _seal(draft_digest="c" * 64), github)

        assert (
            recover_decomposition_seal(github.issue_comments(8), "a" * 64, draft_digest="f" * 64)
            is None
        )

    def test_bindings_recover_with_their_seal_digest(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        seal = _seal(epoch=epoch)
        publish_decomposition_seal(8, seal, github)
        bindings = DecompositionBindings(
            requirements_epoch=epoch,
            seal_digest=seal.seal_digest,
            bindings=(("node-a", 41),),
        )
        publish_decomposition_bindings(8, bindings, github)

        recovered = recover_decomposition_bindings(
            github.issue_comments(8), epoch, seal_digest=seal.seal_digest
        )

        assert recovered == bindings
        assert recovered is not None
        assert recovered.issue_for("node-a") == 41

    def test_transition_receipt_round_trips(self) -> None:
        github = FakeStageGitHub()
        receipt = DecompositionTransitionReceipt(
            requirements_epoch="a" * 64,
            labels_added=("epic", "state:skip"),
            labels_removed=("state:plan-no-go",),
            needs_plan_confirmed=("node-a",),
            summary_confirmed=True,
        )
        publish_decomposition_transition(8, receipt, github)

        assert recover_decomposition_transition(github.issue_comments(8), "a" * 64) == receipt

    def test_draft_publication_is_idempotent_and_recovers_the_tree(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        nodes = [dict(node) for node in _seal(epoch=epoch).nodes]

        first = publish_decomposition_draft(8, epoch, _digest("draft"), nodes, github)
        second = publish_decomposition_draft(8, epoch, _digest("draft"), nodes, github)

        assert first == second == 1
        assert len(github.comments[8]) == 1
        assert recover_decomposition_draft(github.issue_comments(8), epoch) == tuple(nodes)

    def test_large_drafts_are_chunked_and_reassembled(self) -> None:
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        nodes = [dict(node) for node in _seal(epoch=epoch).nodes] * 800

        total = publish_decomposition_draft(8, epoch, _digest("large"), nodes, github)

        assert total > 1
        recovered = recover_decomposition_draft(github.issue_comments(8), epoch)
        assert recovered == tuple(nodes)

    def test_partial_records_reconcile_to_none_until_complete(self) -> None:
        """A missing draft part yields no tree until every part exists."""
        github = FakeStageGitHub()
        epoch = _epoch("one epoch")
        nodes = [dict(node) for node in _seal(epoch=epoch).nodes] * 800
        total = publish_decomposition_draft(8, epoch, _digest("draft"), nodes, github)
        assert total > 1
        # Keep only the first part; the later parts are missing.
        github.comments[8] = [github.comments[8][0]]

        assert recover_decomposition_draft(github.issue_comments(8), epoch) is None

    def test_summary_body_carries_the_ordered_tree(self) -> None:
        body = render_decomposition_summary_body(
            parent_issue=8,
            epoch="a" * 64,
            ordered_issues=(("node-a", 41), ("node-b", 42)),
            layers=(("node-a",), ("node-b",)),
            bindings={"node-a": 41, "node-b": 42},
        )
        assert "node-a: #41" in body
        assert "node-b -> #42" in body
