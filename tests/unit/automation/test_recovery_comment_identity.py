"""Tests for recovered-requirements comment identity checks.

These exercise a real authority boundary: a comment carrying the versioned
recovery marker must be actor-owned, well-formed, and unambiguous before any
mutation targets it.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.comment_identity import CommentAliasConflictError
from hephaestus.automation.recovery_comment_identity import (
    has_valid_recovery_provenance,
    is_recovery_provenance_marker,
    marker_body_error,
    marker_matches_body,
    select_marker_target,
    validate_marker_identities,
    validate_recovery_comment_identities,
)
from hephaestus.automation.requirements_recovery import (
    RECOVERY_PROVENANCE_PREFIX,
    render_recovered_requirements,
)
from hephaestus.automation.review_journal import PLAN_CANONICAL_MARKER

_PLAN_MARKER_LINE = f"{PLAN_CANONICAL_MARKER}\n\nplan body\n"


def _recovery_body(**overrides: object) -> str:
    """Render one valid recovered-requirements body."""
    return render_recovered_requirements(
        source_body="original issue body",
        requirements="the recovered requirement text",
        evidence_binding="a" * 64,
        **overrides,  # type: ignore[arg-type]
    )


def _comments(*bodies: str, owned: bool = True) -> list[dict[str, str]]:
    """Build comment records with the given bodies and one ownership flag."""
    return [{"body": body, "owned": "yes" if owned else "no"} for body in bodies]


def _body_of(comment: dict[str, str]) -> str:
    return comment["body"]


def _owned_of(comment: dict[str, str]) -> bool:
    return comment["owned"] == "yes"


def test_rendered_body_carries_a_versioned_marker() -> None:
    """A real recovery body is recognised by its own family predicate."""
    body = _recovery_body()
    assert body.startswith(RECOVERY_PROVENANCE_PREFIX)
    assert is_recovery_provenance_marker(RECOVERY_PROVENANCE_PREFIX)
    assert has_valid_recovery_provenance(body)


def test_plain_body_is_not_a_recovery_marker() -> None:
    """An ordinary plan body is not mistaken for a recovery marker."""
    assert not has_valid_recovery_provenance(_PLAN_MARKER_LINE)
    assert not is_recovery_provenance_marker(PLAN_CANONICAL_MARKER)


def test_prefix_without_valid_digests_is_rejected() -> None:
    """The bare prefix is not enough; the marker must parse."""
    assert not has_valid_recovery_provenance(f"{RECOVERY_PROVENANCE_PREFIX}not-a-marker -->")


def test_marker_body_error_accepts_a_valid_recovery_body() -> None:
    """A valid recovery body produces no write error.

    This is the regression the module was written for: the strict full-line
    check rejected the versioned marker and the write died with an uncaught
    ValueError.
    """
    assert marker_body_error(_recovery_body(), RECOVERY_PROVENANCE_PREFIX) is None


def test_marker_body_error_rejects_a_malformed_recovery_body() -> None:
    """A body that carries the prefix but cannot parse is still refused."""
    error = marker_body_error(
        f"{RECOVERY_PROVENANCE_PREFIX}v=3:source=zz -->\n\ntext", RECOVERY_PROVENANCE_PREFIX
    )
    assert error is not None
    assert "recovered marker" in error


def test_marker_body_error_keeps_the_exact_rule_for_plan_comments() -> None:
    """The plan marker family keeps its strict first-line contract."""
    assert marker_body_error(_PLAN_MARKER_LINE, PLAN_CANONICAL_MARKER) is None
    assert marker_body_error("plan body with no marker", PLAN_CANONICAL_MARKER) is not None


def test_marker_matches_body_uses_the_family_rule() -> None:
    """Each family gets its own match rule."""
    assert marker_matches_body(_recovery_body(), RECOVERY_PROVENANCE_PREFIX, ())
    assert marker_matches_body(_PLAN_MARKER_LINE, PLAN_CANONICAL_MARKER, (PLAN_CANONICAL_MARKER,))
    assert not marker_matches_body(
        _PLAN_MARKER_LINE, RECOVERY_PROVENANCE_PREFIX, (RECOVERY_PROVENANCE_PREFIX,)
    )


def test_validate_recovery_accepts_one_owned_comment() -> None:
    """The normal case is a single actor-owned recovery comment."""
    validate_recovery_comment_identities(
        _comments(_recovery_body()), body_of=_body_of, owned_of=_owned_of
    )


def test_validate_recovery_ignores_unrelated_comments() -> None:
    """Other markers in the same timeline do not create ambiguity."""
    validate_recovery_comment_identities(
        _comments(_PLAN_MARKER_LINE, _recovery_body()),
        body_of=_body_of,
        owned_of=_owned_of,
    )


def test_validate_recovery_rejects_a_foreign_comment() -> None:
    """A recovery marker authored by someone else is an identity conflict."""
    with pytest.raises(CommentAliasConflictError, match="foreign"):
        validate_recovery_comment_identities(
            _comments(_recovery_body(), owned=False), body_of=_body_of, owned_of=_owned_of
        )


def test_validate_recovery_rejects_a_malformed_comment() -> None:
    """A prefix-carrying comment that cannot parse is rejected."""
    with pytest.raises(CommentAliasConflictError, match="malformed"):
        validate_recovery_comment_identities(
            _comments(f"{RECOVERY_PROVENANCE_PREFIX}broken -->\n\ntext"),
            body_of=_body_of,
            owned_of=_owned_of,
        )


def test_validate_recovery_rejects_duplicate_comments() -> None:
    """Two recovery comments are ambiguous and must not be auto-selected."""
    with pytest.raises(CommentAliasConflictError, match="ambiguous"):
        validate_recovery_comment_identities(
            _comments(_recovery_body(), _recovery_body()),
            body_of=_body_of,
            owned_of=_owned_of,
        )


def test_validate_marker_identities_dispatches_to_the_planning_family() -> None:
    """A plan marker still runs the planning identity validation."""
    validate_marker_identities(
        _comments(_PLAN_MARKER_LINE),
        marker=PLAN_CANONICAL_MARKER,
        planning_marker=True,
        body_of=_body_of,
        owned_of=_owned_of,
    )


def test_validate_marker_identities_dispatches_to_the_recovery_family() -> None:
    """A recovery marker runs the recovery identity validation, not planning's.

    Two recovery comments are ambiguous under the recovery family. The planning
    family would ignore them entirely because they carry no planning marker, so
    this proves the dispatch reached the recovery branch.
    """
    with pytest.raises(CommentAliasConflictError, match="ambiguous recovered-requirements"):
        validate_marker_identities(
            _comments(_recovery_body(), _recovery_body()),
            marker=RECOVERY_PROVENANCE_PREFIX,
            planning_marker=False,
            body_of=_body_of,
            owned_of=_owned_of,
        )


def test_select_marker_target_returns_the_single_recovery_comment() -> None:
    """One candidate is selected without an alias rule."""
    comments = _comments(_recovery_body())
    assert (
        select_marker_target(
            comments,
            marker=RECOVERY_PROVENANCE_PREFIX,
            aliases=(),
            body_of=_body_of,
            owned_of=_owned_of,
        )
        is comments[0]
    )


def test_select_marker_target_returns_none_when_absent() -> None:
    """No candidate is not an error; the caller creates a new comment."""
    assert (
        select_marker_target(
            [],
            marker=RECOVERY_PROVENANCE_PREFIX,
            aliases=(),
            body_of=_body_of,
            owned_of=_owned_of,
        )
        is None
    )


def test_select_marker_target_rejects_duplicates_itself() -> None:
    """Selection enforces the duplicate rule without a prior validation call."""
    with pytest.raises(CommentAliasConflictError, match="ambiguous"):
        select_marker_target(
            _comments(_recovery_body(), _recovery_body()),
            marker=RECOVERY_PROVENANCE_PREFIX,
            aliases=(),
            body_of=_body_of,
            owned_of=_owned_of,
        )


def test_select_marker_target_rejects_a_foreign_candidate() -> None:
    """Selection enforces ownership without a prior validation call."""
    with pytest.raises(CommentAliasConflictError, match="foreign"):
        select_marker_target(
            _comments(_recovery_body(), owned=False),
            marker=RECOVERY_PROVENANCE_PREFIX,
            aliases=(),
            body_of=_body_of,
            owned_of=_owned_of,
        )
