"""Behavior tests for the standalone recovery-comment identity module (#3406).

Regression coverage for PR #3475 review finding F-001 (P064): the module's own
composition — byte-zero marker agreement, bounded mismatch reasons, CR/LF
tolerance, and the owned-one rule through the validation alias and the target
selector — is covered here. The hardened delegated core keeps its own tests in
``test_requirements_recovery.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from hephaestus.automation import (
    recovery_comment_identity as module,
    requirements_recovery as upstream,
)
from hephaestus.automation.recovery_comment_identity import (
    marker_body_error,
    marker_matches_body,
    select_marker_target,
    validate_recovery_comment_identities,
)
from hephaestus.automation.requirements_recovery import (
    RecoveryCommentIdentityError,
    render_recovered_requirements,
)

_SOURCE = "issue body whose requirements were lost"
_REQUIREMENTS = "## Recovered Requirements\n- the planner must recover them\n"
_EVIDENCE = "c" * 64


def _v2() -> tuple[str, str]:
    body = render_recovered_requirements(_SOURCE, _REQUIREMENTS, _EVIDENCE)
    marker, _, _ = body.partition("\n")
    return marker, body


def _v3() -> tuple[str, str]:
    body = render_recovered_requirements(
        _SOURCE,
        _REQUIREMENTS,
        _EVIDENCE,
        issue_title="lost plan",
        repository_revision="a" * 40,
    )
    marker, _, _ = body.partition("\n")
    return marker, body


def _tampered(marker: str, body: str) -> tuple[str, str]:
    pivot = marker.index("requirements=") + len("requirements=")
    flipped = "0" if marker[pivot] != "0" else "f"
    bad_marker = marker[:pivot] + flipped + marker[pivot + 1 :]
    return bad_marker, bad_marker + body[len(marker) :]


def test_module_reexports_the_shared_marker_family() -> None:
    """The standalone module imports, not duplicates, the shared core (F-002/F-003)."""
    assert module.RECOVERY_PROVENANCE_PREFIX is upstream.RECOVERY_PROVENANCE_PREFIX
    assert module.RecoveryCommentIdentityError is upstream.RecoveryCommentIdentityError


_MARK, _BODY = _v2()
_MARK3, _BODY3 = _v3()
_BAD_MARK, _BAD_BODY = _tampered(_MARK, _BODY)


@pytest.mark.parametrize(
    ("marker", "body", "expected_match", "expected_error"),
    [
        pytest.param(_MARK, _BODY, True, None, id="exact-v2"),
        pytest.param(_MARK3, _BODY3, True, None, id="exact-v3-contextual"),
        pytest.param(_MARK, _BODY.replace("\n", "\r\n", 1), True, None, id="crlf-first-line"),
        pytest.param(
            _MARK,
            _BODY3,
            False,
            "marker does not match the first body line",
            id="first-line-mismatch",
        ),
        pytest.param(
            _MARK,
            _MARK,
            False,
            "marker is not followed by a body",
            id="marker-without-body",
        ),
        pytest.param(
            _BAD_MARK,
            _BAD_BODY,
            False,
            "marker provenance is malformed or disagrees with the body",
            id="tampered-requirements-digest",
        ),
    ],
)
def test_marker_agreement_and_bounded_error_reasons(
    marker: str, body: str, expected_match: bool, expected_error: str | None
) -> None:
    """The boolean path and the reason path stay strictly consistent."""
    assert marker_matches_body(marker, body) is expected_match
    assert marker_body_error(marker, body) == expected_error


@dataclass(frozen=True)
class _Comment:
    body: str
    owned: bool


def _body_of(comment: _Comment) -> str:
    return comment.body


def _owned_of(comment: _Comment) -> bool:
    return comment.owned


def test_empty_journal_selects_nothing() -> None:
    """A journal with no recovery claim returns ``None``."""
    assert validate_recovery_comment_identities([], body_of=_body_of, owned_of=_owned_of) is None
    assert select_marker_target([], body_of=_body_of, owned_of=_owned_of) is None


@pytest.mark.parametrize("version_case", ["v2", "v3"])
def test_one_owned_recovery_comment_is_selected(version_case: str) -> None:
    """The one actor-owned marker comment is the validated target."""
    _, body = _v2() if version_case == "v2" else _v3()
    target = _Comment(body, True)
    journal = [
        _Comment("unrelated drive-by comment", False),
        target,
        _Comment("a later plain discussion", True),
    ]
    selection = validate_recovery_comment_identities(journal, body_of=_body_of, owned_of=_owned_of)
    assert selection is not None
    assert selection.comment is target
    assert selection.provenance.evidence_digest == _EVIDENCE
    assert select_marker_target(journal, body_of=_body_of, owned_of=_owned_of) is target


def test_foreign_marker_claim_is_an_identity_error() -> None:
    """A marker the actor does not own fails closed."""
    _, body = _v2()
    journal = [_Comment(body, False)]
    with pytest.raises(RecoveryCommentIdentityError, match="foreign"):
        validate_recovery_comment_identities(journal, body_of=_body_of, owned_of=_owned_of)
    with pytest.raises(RecoveryCommentIdentityError, match="foreign"):
        select_marker_target(journal, body_of=_body_of, owned_of=_owned_of)


def test_duplicate_owned_recovery_comments_are_an_identity_error() -> None:
    """Two owned marker comments are ambiguous and fail closed."""
    _, body = _v2()
    journal = [_Comment(body, True), _Comment(body, True)]
    with pytest.raises(RecoveryCommentIdentityError, match="duplicate"):
        validate_recovery_comment_identities(journal, body_of=_body_of, owned_of=_owned_of)
    with pytest.raises(RecoveryCommentIdentityError, match="duplicate"):
        select_marker_target(journal, body_of=_body_of, owned_of=_owned_of)
