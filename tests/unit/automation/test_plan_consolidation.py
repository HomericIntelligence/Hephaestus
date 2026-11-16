"""Operator-authorized duplicate plan/review consolidation (#3307).

The matrix pins the Comet #705 shape: one actor publishes two qualifying
plan/review pairs through different aliases. Consolidation binds one merged
epoch to the exact source records, preserves both reviews' findings in the
operator-merged content, archives each superseded comment before deleting it,
forces a fresh independent review, and replays exactly.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hephaestus.automation.comment_identity import marker_aliases_in_body
from hephaestus.automation.plan_consolidation import (
    ArchiveMarkerCollisionError,
    ConsolidationCoverageError,
    ConsolidationDeletionError,
    ConsolidationDriftError,
    ConsolidationLatchError,
    ConsolidationPlan,
    ConsolidationRawPatchError,
    ConsolidationRequest,
    ConsolidationStateTokenError,
    ForeignPlanningClaimError,
    PlanningRoleClaim,
    execute_consolidation,
    extract_consolidation_archive,
    plan_consolidation,
    planning_role_inventory,
    render_consolidation_archive,
)
from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    comment_marker_aliases,
)
from hephaestus.automation.review_journal import (
    IssueComment,
    parse_consolidation_provenance as parse_provenance_line,
    parse_plan_review_state,
    plan_fingerprint,
    render_consolidation_provenance,
    render_current_plan,
    render_current_review,
)

ACTOR = "mvillmow"
ISSUE_NUMBER = 705
REPO = ("LLM360", "comet")

ATHENA_PLAN_ID = 5444284559
ATHENA_REVIEW_ID = 5444310901
SHARED_PLAN_ID = 5446247620
SHARED_REVIEW_ID = 5446247883

ATHENA_PLAN_BODY = (
    "<!-- athena:plan-issue -->\n# Implementation Plan\n\nAthena-era plan requirements\n"
)
ATHENA_REVIEW_BODY = "<!-- athena:issue-review -->\n## Plan Review\n\nAthena-era review findings\n"
SHARED_PLAN_BODY = render_current_plan("Hephaestus plan requirements", revision=3)
SHARED_REVIEW_BODY = render_current_review("Hephaestus review findings", revision=3)

MERGED_PLAN = "One merged plan that preserves both plans' requirements.\n"
MERGED_REVIEW = "Merged review findings without a verdict token.\n"
LABELS = ("state:plan-no-go",)


def _comment(
    body: str,
    comment_id: int,
    *,
    owned: bool = True,
    login: str = ACTOR,
) -> IssueComment:
    return IssueComment(
        body=body,
        author_login=login,
        viewer_did_author=owned,
        database_id=comment_id,
    )


def _journal() -> list[IssueComment]:
    """Return the #705 shape: two same-actor pairs across two alias families."""
    return [
        _comment(ATHENA_PLAN_BODY, ATHENA_PLAN_ID),
        _comment(ATHENA_REVIEW_BODY, ATHENA_REVIEW_ID),
        _comment(SHARED_PLAN_BODY, SHARED_PLAN_ID),
        _comment(SHARED_REVIEW_BODY, SHARED_REVIEW_ID),
    ]


def _request(**overrides: object) -> ConsolidationRequest:
    fields: dict[str, object] = {
        "issue_number": ISSUE_NUMBER,
        "issue_owner": REPO[0],
        "issue_name": REPO[1],
        "keep_plan_comment_id": SHARED_PLAN_ID,
        "keep_review_comment_id": SHARED_REVIEW_ID,
        "supersede_plan_comment_ids": (ATHENA_PLAN_ID,),
        "supersede_review_comment_ids": (ATHENA_REVIEW_ID,),
        "plan_content": MERGED_PLAN,
        "review_content": MERGED_REVIEW,
    }
    fields.update(overrides)
    return ConsolidationRequest(**fields)  # type: ignore[arg-type]


def _plan(
    comments: list[IssueComment] | None = None,
    *,
    request: ConsolidationRequest | None = None,
    labels: tuple[str, ...] = LABELS,
    issue_body: str = "Issue requirements body\n",
) -> ConsolidationPlan:
    return plan_consolidation(
        comments if comments is not None else _journal(),
        issue_body,
        labels,
        request or _request(),
    )


class FakeGitHub:
    """Call-recording fake with the exact seam semantics the executor needs."""

    def __init__(
        self,
        comments: list[IssueComment],
        labels: tuple[str, ...],
        issue_body: str,
    ) -> None:
        self._comments = list(comments)
        self._next_id = max((c.database_id or 0 for c in comments), default=0) + 1
        self.labels = set(labels)
        self.issue_body = issue_body
        self.calls: list[tuple[object, ...]] = []

    def fetch_comments(self) -> list[IssueComment]:
        self.calls.append(("fetch_comments",))
        return list(self._comments)

    def fetch_labels(self) -> list[str]:
        self.calls.append(("fetch_labels",))
        return sorted(self.labels)

    def fetch_issue_body(self) -> str:
        self.calls.append(("fetch_issue_body",))
        return self.issue_body

    def append_comment(self, body: str) -> None:
        self.calls.append(("append_comment", body.partition("\n")[0]))
        self._comments.append(_comment(body, self._next_id))
        self._next_id += 1

    def delete_comment(self, comment_id: int) -> None:
        self.calls.append(("delete_comment", comment_id))
        self._comments = [
            comment for comment in self._comments if comment.database_id != comment_id
        ]

    def upsert_owned_comment(self, marker: str, body: str) -> None:
        self.calls.append(("upsert_owned_comment", marker))
        aliases = comment_marker_aliases(marker)
        matches = [
            comment
            for comment in self._comments
            if comment.viewer_did_author and marker_aliases_in_body(comment.body, aliases)
        ]
        if matches:
            target = matches[-1]
            index = self._comments.index(target)
            self._comments[index] = replace(target, body=body)
            return
        self._comments.append(_comment(body, self._next_id))
        self._next_id += 1

    def edit_labels(self, add: tuple[str, ...], remove: tuple[str, ...]) -> None:
        self.calls.append(("edit_labels", tuple(add), tuple(remove)))
        self.labels -= set(remove)
        self.labels |= set(add)

    def seams(self) -> dict[str, object]:
        return {
            "fetch_comments": self.fetch_comments,
            "fetch_labels": self.fetch_labels,
            "fetch_issue_body": self.fetch_issue_body,
            "append_comment": self.append_comment,
            "delete_comment": self.delete_comment,
            "upsert_owned_comment": self.upsert_owned_comment,
            "edit_labels": self.edit_labels,
        }

    def mutation_kinds(self) -> list[str]:
        mutating = {"append_comment", "delete_comment", "upsert_owned_comment", "edit_labels"}
        return [str(call[0]) for call in self.calls if call[0] in mutating]


def test_same_actor_duplicate_pairs_plan_one_consolidation() -> None:
    """The #705 shape plans one merged epoch bound to all four source records."""
    plan = _plan()

    assert plan.epoch == 4
    assert plan.deletions == (ATHENA_PLAN_ID, ATHENA_REVIEW_ID)
    assert [archive.claim.comment_id for archive in plan.archives] == [
        ATHENA_PLAN_ID,
        ATHENA_REVIEW_ID,
    ]
    assert plan.archives[0].marker == ("<!-- hephaestus-plan-history:revision=1:kind=plan -->")
    assert plan.archives[1].marker == ("<!-- hephaestus-plan-history:revision=1:kind=review -->")
    provenance = parse_provenance_line(plan.provenance_line)
    assert provenance is not None
    assert (provenance.issue_owner, provenance.issue_name, provenance.issue_number) == (
        "LLM360",
        "comet",
        705,
    )
    assert provenance.epoch == 4
    bound = [(source.role, source.comment_id, source.alias_tag) for source in provenance.sources]
    assert bound == [
        ("plan", SHARED_PLAN_ID, "shared"),
        ("plan", ATHENA_PLAN_ID, "athena"),
        ("review", ATHENA_REVIEW_ID, "athena"),
        ("review", SHARED_REVIEW_ID, "shared"),
    ]


def test_consolidated_plan_body_carries_the_sealed_binding() -> None:
    """The keep-plan body carries the shared marker, epoch, and fingerprints."""
    plan = _plan()

    assert plan.keep_plan_body.startswith(PLAN_CANONICAL_MARKER)
    assert "<!-- revision: 4 -->" in plan.keep_plan_body
    assert plan.provenance_line in plan.keep_plan_body
    assert MERGED_PLAN.strip() in plan.keep_plan_body
    fingerprints = [
        line
        for line in plan.keep_plan_body.splitlines()
        if line.startswith("<!-- prior-plan-fingerprints:")
    ]
    assert len(fingerprints) == 1
    assert plan_fingerprint(ATHENA_PLAN_BODY) in fingerprints[0]
    assert plan_fingerprint(SHARED_PLAN_BODY) in fingerprints[0]


def test_consolidated_review_forces_a_fresh_review() -> None:
    """The keep-review body carries no terminal state token."""
    plan = _plan()

    assert plan.keep_review_body.startswith(PLAN_REVIEW_CANONICAL_MARKER)
    assert "<!-- revision: 4 -->" in plan.keep_review_body
    assert parse_plan_review_state(plan.keep_review_body) is None


def test_inventory_enumerates_duplicate_claims_without_selecting() -> None:
    """The pure inventory reports every claim; no code path picks a winner."""
    claims = planning_role_inventory(_journal())

    assert [claim.comment_id for claim in claims] == [
        ATHENA_PLAN_ID,
        ATHENA_REVIEW_ID,
        SHARED_PLAN_ID,
        SHARED_REVIEW_ID,
    ]
    assert all(claim.owned for claim in claims)
    assert sorted(claim.role for claim in claims) == ["plan", "plan", "review", "review"]


def test_foreign_ownership_aborts_before_any_mutation() -> None:
    """A foreign plan/review claim refuses the consolidation outright."""
    comments = _journal()
    comments[0] = _comment(ATHENA_PLAN_BODY, ATHENA_PLAN_ID, owned=False, login="someone-else")

    with pytest.raises(ForeignPlanningClaimError, match=str(ATHENA_PLAN_ID)):
        _plan(comments)


def test_mixed_alias_family_binds_each_source_alias() -> None:
    """Each source record carries the alias tag of the marker it was written with."""
    comments = [
        _comment(ATHENA_PLAN_BODY, ATHENA_PLAN_ID),
        _comment(
            "<!-- hephaestus-plan-review:canonical -->\n## Plan Review\n\nLegacy review\n",
            ATHENA_REVIEW_ID,
        ),
        _comment(SHARED_PLAN_BODY, SHARED_PLAN_ID),
        _comment(SHARED_REVIEW_BODY, SHARED_REVIEW_ID),
    ]

    plan = _plan(comments)

    provenance = parse_provenance_line(plan.provenance_line)
    assert provenance is not None
    assert provenance.sources[0].alias_tag == "shared"
    assert provenance.sources[1].alias_tag == "athena"
    assert provenance.sources[2].alias_tag == "hephaestus"


def test_unnamed_owned_claim_aborts_coverage() -> None:
    """Every owned claim must be named as keep or supersede."""
    comments = _journal()
    comments.append(_comment(render_current_plan("Third plan", revision=1), 999999))

    with pytest.raises(ConsolidationCoverageError, match="999999"):
        _plan(comments)


def test_same_kind_same_revision_archive_collision_aborts() -> None:
    """Two superseded plans without revisions share one archive marker."""
    comments = [
        _comment(ATHENA_PLAN_BODY, ATHENA_PLAN_ID),
        _comment(
            render_current_plan("Second revision-less plan", revision=1),
            999998,
        ),
        _comment(SHARED_PLAN_BODY, SHARED_PLAN_ID),
        _comment(SHARED_REVIEW_BODY, SHARED_REVIEW_ID),
    ]

    with pytest.raises(ArchiveMarkerCollisionError, match="share revision 1"):
        _plan(
            comments,
            request=_request(
                supersede_plan_comment_ids=(ATHENA_PLAN_ID, 999998),
                supersede_review_comment_ids=(),
            ),
        )


def test_state_token_in_merged_review_aborts() -> None:
    """A merged review that ends in a state token would transfer a GO."""
    with pytest.raises(ConsolidationStateTokenError, match="state token"):
        _plan(request=_request(review_content="Merged review\n\nstate:plan-go\n"))


def test_raw_patch_in_merged_plan_aborts() -> None:
    """A merged plan may not embed unified-diff material."""
    with pytest.raises(ConsolidationRawPatchError, match="unified-diff"):
        _plan(request=_request(plan_content="Plan\n\n```diff\n--- a\n+++ b\n```\n"))


@pytest.mark.parametrize("latch", ["state:skip", "state:blocked"])
def test_operator_latch_labels_abort(latch: str) -> None:
    """An operator latch label refuses the consolidation."""
    with pytest.raises(ConsolidationLatchError, match=latch):
        _plan(labels=(latch,))


def test_provenance_line_round_trips() -> None:
    """The provenance header parses back to the exact binding."""
    line = render_consolidation_provenance(
        issue_owner="LLM360",
        issue_name="comet",
        issue_number=705,
        requirements_digest="a" * 64,
        epoch=4,
        sources=(_plan().sources[0],),
    )

    parsed = parse_provenance_line(line)
    assert parsed is not None
    assert parsed.requirements_digest == "a" * 64
    assert parsed.epoch == 4
    assert parsed.sources[0].role == "plan"


@pytest.mark.parametrize(
    "source_body",
    [
        "Simple body\n",
        "Body without trailing newline",
        "```text\nembedded fence\n```\n\nafter\n",
        "````\nlonger fence\n````\n",
        "multi\nline\nbody\n\nwith blank lines\n",
    ],
)
def test_archive_round_trip_recovers_exact_bytes(source_body: str) -> None:
    """Archive extraction recovers the exact source body byte for byte."""
    claim = PlanningRoleClaim(
        comment_id=1,
        author_login=ACTOR,
        role="plan",
        marker="",
        alias_tag="shared",
        revision=1,
        digest=plan_fingerprint(f"{PLAN_CANONICAL_MARKER}\n\n{source_body}"),
        owned=True,
        created_at="",
        updated_at="",
    )

    archive = render_consolidation_archive(claim, source_body)

    assert extract_consolidation_archive(archive) == source_body


def test_archive_hides_embedded_planning_markers() -> None:
    """A fenced source marker stays invisible to top-level marker readers."""
    archive = render_consolidation_archive(
        planning_role_inventory([_comment(ATHENA_PLAN_BODY, ATHENA_PLAN_ID)])[0],
        ATHENA_PLAN_BODY,
    )

    from hephaestus.automation.comment_identity import (
        is_immutable_history_artifact,
        marker_aliases_in_body,
    )
    from hephaestus.automation.protocol import PLAN_CANONICAL_MARKERS

    assert is_immutable_history_artifact(archive)
    assert marker_aliases_in_body(archive, PLAN_CANONICAL_MARKERS) == ()
    assert extract_consolidation_archive(archive) == ATHENA_PLAN_BODY


def test_executor_writes_archives_then_deletes_then_upserts() -> None:
    """The executor order preserves source evidence before any removal."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    plan = _plan()

    outcome = execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]

    assert outcome.already_consolidated is False
    assert outcome.appended_archives == 2
    assert outcome.deleted_comment_ids == (ATHENA_PLAN_ID, ATHENA_REVIEW_ID)
    kinds = [call[0] for call in fake.calls if call[0] != "fetch_comments"]
    assert kinds.index("append_comment") < kinds.index("delete_comment")
    assert kinds.index("delete_comment") < kinds.index("upsert_owned_comment")
    final_bodies = [comment.body for comment in fake.fetch_comments()]
    plan_bodies = [body for body in final_bodies if body.startswith(PLAN_CANONICAL_MARKER)]
    review_bodies = [body for body in final_bodies if body.startswith(PLAN_REVIEW_CANONICAL_MARKER)]
    assert plan_bodies == [plan.keep_plan_body]
    assert review_bodies == [plan.keep_review_body]


def test_executor_moves_a_historical_go_off_the_issue() -> None:
    """A historical GO label becomes plan-no-go; the GO is never transferred."""
    fake = FakeGitHub(_journal(), ("state:plan-go",), "Issue requirements body\n")
    plan = _plan(labels=("state:plan-go",))

    outcome = execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]

    assert outcome.labels_edited is True
    assert fake.labels == {"state:plan-no-go"}


def test_executor_keeps_labels_without_a_historical_go() -> None:
    """No label transition is planned or executed without a GO label."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")

    outcome = execute_consolidation(_plan(), **fake.seams())  # type: ignore[arg-type]

    assert outcome.labels_edited is False
    assert fake.labels == set(LABELS)


def test_executor_rejects_source_digest_drift_before_writes() -> None:
    """A changed source body aborts before any mutation."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    plan = _plan()
    comments = fake.fetch_comments()
    index = next(
        index for index, comment in enumerate(comments) if comment.database_id == SHARED_PLAN_ID
    )
    fake._comments[index] = replace(
        comments[index], body=render_current_plan("Tampered plan", revision=3)
    )

    with pytest.raises(ConsolidationDriftError, match=str(SHARED_PLAN_ID)):
        execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]

    assert fake.mutation_kinds() == []


def test_executor_rejects_requirements_drift() -> None:
    """A changed issue body aborts before any mutation."""
    fake = FakeGitHub(_journal(), LABELS, "Changed issue requirements\n")
    plan = _plan()

    with pytest.raises(ConsolidationDriftError, match="issue requirements"):
        execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]

    assert fake.mutation_kinds() == []


def test_uncertain_delete_names_the_remaining_ids() -> None:
    """An unconfirmed delete stops the run and names the unconfirmed work."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    plan = _plan()
    seams = fake.seams()

    def refusing_delete(comment_id: int) -> None:
        fake.calls.append(("delete_comment", comment_id))

    seams["delete_comment"] = refusing_delete

    with pytest.raises(ConsolidationDeletionError, match=str(ATHENA_PLAN_ID)):
        execute_consolidation(plan, **seams)  # type: ignore[arg-type]


def test_partial_failure_rerun_converges() -> None:
    """A run that stops after the deletes converges on the next run."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    plan = _plan()

    class FlakyUpsert(FakeGitHub):
        def __init__(self, *args: object, **kwargs: object) -> None:
            super().__init__(*args, **kwargs)  # type: ignore[arg-type]
            self._upserts_to_fail = 1

        def upsert_owned_comment(self, marker: str, body: str) -> None:
            if self._upserts_to_fail > 0:
                self._upserts_to_fail -= 1
                raise RuntimeError("github unavailable after the deletes")
            super().upsert_owned_comment(marker, body)

    failing = FlakyUpsert(fake._comments, LABELS, fake.issue_body)
    with pytest.raises(RuntimeError, match="github unavailable"):
        execute_consolidation(plan, **failing.seams())  # type: ignore[arg-type]
    assert len(failing._comments) == 4  # two keeps plus two archives

    recovered = plan_consolidation(
        failing.fetch_comments(),
        failing.issue_body,
        failing.fetch_labels(),
        _request(),
    )
    assert recovered.epoch == plan.epoch
    assert recovered.provenance_line == plan.provenance_line
    assert recovered.deletions == ()

    outcome = execute_consolidation(recovered, **failing.seams())  # type: ignore[arg-type]
    assert outcome.already_consolidated is False
    final = planning_role_inventory(failing.fetch_comments())
    assert [claim.comment_id for claim in final if claim.owned] == [
        SHARED_PLAN_ID,
        SHARED_REVIEW_ID,
    ]


def test_completed_replay_performs_no_mutation() -> None:
    """A second identical run reports already-consolidated without writes."""
    fake = FakeGitHub(_journal(), ("state:plan-go",), "Issue requirements body\n")
    first = _plan(labels=("state:plan-go",))
    execute_consolidation(first, **fake.seams())  # type: ignore[arg-type]

    replay = plan_consolidation(
        fake.fetch_comments(),
        fake.issue_body,
        fake.fetch_labels(),
        _request(),
    )
    assert replay.epoch == first.epoch
    assert replay.provenance_line == first.provenance_line

    before = len(fake.calls)
    outcome = execute_consolidation(replay, **fake.seams())  # type: ignore[arg-type]

    assert outcome.already_consolidated is True
    assert outcome.appended_archives == 0
    assert outcome.deleted_comment_ids == ()
    assert outcome.labels_edited is False
    assert fake.mutation_kinds()[before:] == []


def test_consolidated_state_survives_timeline_compaction() -> None:
    """Compaction re-renders the consolidated plan without losing the binding."""
    from hephaestus.automation.issue_timeline import plan_issue_timeline_compaction

    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    execute_consolidation(_plan(), **fake.seams())  # type: ignore[arg-type]

    compaction = plan_issue_timeline_compaction(fake.fetch_comments())

    assert compaction.plan_needs_update is False
    assert compaction.review_needs_update is False
    assert compaction.delete_comment_ids == ()


def test_unknown_planning_marker_cannot_become_an_alias_tag() -> None:
    """An unrecognized marker refuses an alias tag."""
    from hephaestus.automation.plan_consolidation import ConsolidationError, alias_tag_for

    with pytest.raises(ConsolidationError, match="accepted"):
        alias_tag_for("plan", "<!-- unknown:marker -->")


def test_inventory_refuses_a_comment_that_claims_both_roles() -> None:
    """A dual-role body cannot be classified, so the inventory refuses it."""
    from hephaestus.automation.plan_consolidation import UnverifiablePlanningClaimError

    dual = _comment(
        "<!-- HomericIntelligence:plan-issue -->\n# Plan\n\n"
        "<!-- HomericIntelligence:issue-review -->\n",
        1,
    )

    with pytest.raises(UnverifiablePlanningClaimError, match="both plan and review"):
        planning_role_inventory([dual])


def test_inventory_refuses_a_claim_without_a_comment_id() -> None:
    """A claim whose comment id is unknown cannot be named or deleted."""
    from hephaestus.automation.plan_consolidation import UnverifiablePlanningClaimError

    nameless = IssueComment(body=ATHENA_PLAN_BODY, viewer_did_author=True, database_id=None)

    with pytest.raises(UnverifiablePlanningClaimError, match="without a comment id"):
        planning_role_inventory([nameless])


def test_archive_extraction_ignores_non_archive_bodies() -> None:
    """Extraction recovers nothing from bodies that are not history archives."""
    assert extract_consolidation_archive(ATHENA_PLAN_BODY) is None
    plain_history = "<!-- hephaestus-plan-history:revision=1:kind=plan -->\nno payload"
    assert extract_consolidation_archive(plain_history) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"keep_plan_comment_id": 5446247883, "keep_review_comment_id": 5446247883},
        {"supersede_plan_comment_ids": (ATHENA_PLAN_ID, SHARED_PLAN_ID)},
        {"plan_content": "   \n"},
        {"review_content": ""},
    ],
)
def test_inconsistent_requests_fail_closed(overrides: dict[str, object]) -> None:
    """Internally inconsistent requests refuse before any plan is produced."""
    from hephaestus.automation.plan_consolidation import ConsolidationRequestError

    with pytest.raises(ConsolidationRequestError):
        _plan(request=_request(**overrides))


def test_existing_different_archive_for_one_marker_aborts() -> None:
    """A journal archive that names the same marker with other content aborts."""
    from hephaestus.automation.review_journal import HISTORY_MARKER

    comments = _journal()
    plan = _plan()
    stale = next(archive for archive in plan.archives)
    comments.append(
        _comment(
            f"{HISTORY_MARKER.format(revision=stale.claim.revision, kind=stale.claim.role)}\n"
            "an unrelated plain archive\n",
            999997,
        )
    )

    with pytest.raises(ArchiveMarkerCollisionError, match="different history archive"):
        plan_consolidation(comments, "Issue requirements body\n", LABELS, _request())


def test_completed_body_without_source_fingerprints_fails_closed() -> None:
    """A replay whose keep body lost a superseded digest refuses to re-bind."""
    comments = _journal()
    fake = FakeGitHub(comments, LABELS, "Issue requirements body\n")
    plan = _plan()
    execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]

    comments = fake.fetch_comments()
    keep_index = next(index for index, c in enumerate(comments) if c.database_id == SHARED_PLAN_ID)
    stripped_body = "\n".join(
        line
        for line in comments[keep_index].body.splitlines()
        if not line.startswith("<!-- prior-plan-fingerprints:")
    )
    comments[keep_index] = replace(comments[keep_index], body=stripped_body)

    from hephaestus.automation.plan_consolidation import ConsolidationError

    with pytest.raises(ConsolidationError, match="fingerprint"):
        plan_consolidation(comments, "Issue requirements body\n", LABELS, _request())


def test_execute_refuses_a_new_foreign_claim() -> None:
    """A foreign claim that appears mid-run aborts the executor."""
    fake = FakeGitHub(_journal(), LABELS, "Issue requirements body\n")
    plan = _plan()
    original = fake.fetch_comments

    def foreign_mid_run() -> list[IssueComment]:
        comments = original()
        if any(comment.body.startswith("<!-- hephaestus-plan-history:") for comment in comments):
            comments.append(
                _comment(
                    render_current_plan("Foreign plan", revision=1),
                    999996,
                    owned=False,
                    login="someone-else",
                )
            )
        return comments

    fake.fetch_comments = foreign_mid_run  # type: ignore[method-assign]

    from hephaestus.automation.plan_consolidation import ForeignPlanningClaimError

    with pytest.raises(ForeignPlanningClaimError):
        execute_consolidation(plan, **fake.seams())  # type: ignore[arg-type]
    assert fake.mutation_kinds().count("append_comment") >= 0
