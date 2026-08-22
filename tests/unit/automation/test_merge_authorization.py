"""Behavior-first tests for explicit operator merge authorization."""

from __future__ import annotations

from typing import Any

import pytest

from hephaestus.automation.merge_authorization import (
    MERGE_AUTHORIZATION_MARKER,
    MergeAuthorizationStatus,
    resolve_merge_authorization,
)

HEAD = "a" * 40


def _review(
    review_id: str = "R1",
    *,
    head: str = HEAD,
    author: str = "operator",
    author_type: str = "User",
    state: str = "APPROVED",
    body: str = MERGE_AUTHORIZATION_MARKER,
    database_id: int | str | None = 1,
    submitted_at: str | None = "2026-08-08T00:00:00Z",
    updated_at: str | None = "2026-08-08T00:00:00Z",
    includes_created_edit: bool = False,
    last_edited_at: str | None = None,
    viewer_did_author: bool = False,
) -> dict[str, Any]:
    """Build one native review-shaped resolver input."""
    return {
        "id": review_id,
        "fullDatabaseId": database_id,
        "body": body,
        "state": state,
        "submittedAt": submitted_at,
        "updatedAt": updated_at,
        "includesCreatedEdit": includes_created_edit,
        "lastEditedAt": last_edited_at,
        "viewerDidAuthor": viewer_did_author,
        "author": {"login": author, "__typename": author_type},
        "commit": {"oid": head},
    }


def _resolve(reviews: tuple[dict[str, Any], ...]) -> Any:
    """Resolve test reviews with a trusted write-capable operator."""
    return resolve_merge_authorization(
        reviews,
        repository="org/repo",
        pr_number=12,
        head_sha=HEAD,
        automation_login="hephaestus[bot]",
        permission_for_actor=lambda _login: "WRITE",
    )


def test_exact_marker_and_native_identity_authorize() -> None:
    """One exact-head unedited operator approval produces a capability."""
    resolution = _resolve((_review(),))

    assert resolution.status is MergeAuthorizationStatus.AUTHORIZED
    assert resolution.authorization is not None
    assert resolution.authorization.review_id == "R1"
    assert resolution.authorization.review_database_id == 1
    assert resolution.authorization.body_digest


def test_noncanonical_marker_is_inert() -> None:
    """Only the exact marker participates in authorization."""
    resolution = _resolve((_review(body=f"{MERGE_AUTHORIZATION_MARKER}\n"),))

    assert resolution.status is MergeAuthorizationStatus.ABSENT


def test_review_commit_oid_must_match_live_head() -> None:
    """An operator approval for an older head cannot authorize this head."""
    resolution = _resolve((_review(head="b" * 40),))

    assert resolution.status is MergeAuthorizationStatus.STALE


def test_null_review_author_is_untrusted_without_vetoing_valid_approval() -> None:
    """A deleted GitHub actor is untrusted, not a malformed-snapshot outage."""
    deleted_actor = _review("deleted")
    deleted_actor["author"] = None

    resolution = _resolve((_review("trusted"), deleted_actor))

    assert resolution.status is MergeAuthorizationStatus.AUTHORIZED
    assert resolution.authorization is not None
    assert resolution.authorization.review_id == "trusted"


def test_permission_lookup_is_deferred_and_casefold_cached() -> None:
    """Only current human candidates consume one permission read per actor."""
    calls: list[str] = []

    def permission_for_actor(login: str) -> str:
        calls.append(login)
        return "WRITE"

    resolution = resolve_merge_authorization(
        (
            _review("stale", head="b" * 40, author="Stale"),
            _review("bot", author="automation[bot]"),
            _review("one", author="Operator", submitted_at="2026-08-08T00:00:00Z"),
            _review("two", author="operator", submitted_at="2026-08-08T00:01:00Z"),
        ),
        repository="org/repo",
        pr_number=12,
        head_sha=HEAD,
        automation_login="hephaestus[bot]",
        permission_for_actor=permission_for_actor,
    )

    assert resolution.status is MergeAuthorizationStatus.AUTHORIZED
    assert resolution.authorization is not None
    assert resolution.authorization.review_id == "two"
    assert calls == ["Operator"]


def test_edited_canonical_review_is_replayed() -> None:
    """A trusted review edited after creation is never accepted."""
    resolution = _resolve((_review(includes_created_edit=True),))

    assert resolution.status is MergeAuthorizationStatus.REPLAYED


def test_automation_actor_cannot_authorize() -> None:
    """The authenticated automation identity cannot self-authorize."""
    resolution = resolve_merge_authorization(
        (_review(author="hephaestus[bot]"),),
        repository="org/repo",
        pr_number=12,
        head_sha=HEAD,
        automation_login="hephaestus[bot]",
        permission_for_actor=lambda _login: "ADMIN",
    )

    assert resolution.status is MergeAuthorizationStatus.UNTRUSTED


def test_duplicate_review_id_is_replayed() -> None:
    """Duplicate native identities in one snapshot fail closed."""
    resolution = _resolve((_review(), _review()))

    assert resolution.status is MergeAuthorizationStatus.REPLAYED


def test_malformed_trusted_current_candidate_is_replayed_even_with_valid_approval() -> None:
    """A malformed trusted current review vetoes a valid approval."""
    malformed = _review("R2", submitted_at=None)
    resolution = _resolve((_review("R1"), malformed))

    assert resolution.status is MergeAuthorizationStatus.REPLAYED
    assert resolution.authorization is None


def test_unclassifiable_service_node_raises() -> None:
    """Missing actor or commit provenance is unavailable, not a veto state."""
    malformed = _review()
    malformed.pop("author")

    with pytest.raises(ValueError, match="author"):
        _resolve((malformed,))


def test_dismissed_review_is_revoked() -> None:
    """A trusted current-head dismissal revokes the marked approval."""
    resolution = _resolve((_review(state="DISMISSED"),))

    assert resolution.status is MergeAuthorizationStatus.REVOKED


def test_later_changes_requested_supersedes_marked_approval() -> None:
    """The author's latest opinionated current-head review controls authorization."""
    marked_approval = _review(
        "R1",
        submitted_at="2026-08-08T00:00:00Z",
        updated_at="2026-08-08T00:00:00Z",
    )
    later_changes_requested = _review(
        "R2",
        state="CHANGES_REQUESTED",
        body="Blocking changes remain.",
        submitted_at="2026-08-08T00:01:00Z",
        updated_at="2026-08-08T00:01:00Z",
    )

    # Deliberately reverse snapshot order: submittedAt, not input position,
    # establishes which opinionated review is latest.
    resolution = _resolve((later_changes_requested, marked_approval))

    assert resolution.status is MergeAuthorizationStatus.REVOKED
    assert resolution.authorization is None


def test_multiple_active_trusted_reviews_are_ambiguous() -> None:
    """Two distinct current-head approvals require operator cleanup."""
    resolution = _resolve((_review("R1"), _review("R2", author="second-operator", database_id="2")))

    assert resolution.status is MergeAuthorizationStatus.AMBIGUOUS


def test_nullable_and_decimal_database_ids_are_normalized() -> None:
    """GitHub BigInt representations normalize into the immutable capability."""
    resolution = _resolve((_review(database_id="1"),))
    assert resolution.authorization is not None
    assert resolution.authorization.review_database_id == 1

    no_database_id = _resolve((_review(database_id=None),))
    assert no_database_id.authorization is not None
    assert no_database_id.authorization.review_database_id is None


def test_low_permission_operator_is_untrusted() -> None:
    """Current write access is required even for a human User actor."""
    resolution = resolve_merge_authorization(
        (_review(),),
        repository="org/repo",
        pr_number=12,
        head_sha=HEAD,
        automation_login="hephaestus[bot]",
        permission_for_actor=lambda _login: "READ",
    )

    assert resolution.status is MergeAuthorizationStatus.UNTRUSTED


def test_authorization_value_is_immutable() -> None:
    """The capability cannot be altered after trusted resolution."""
    resolution = _resolve((_review(),))
    assert resolution.authorization is not None

    with pytest.raises(AttributeError):
        resolution.authorization.review_id = "R2"
