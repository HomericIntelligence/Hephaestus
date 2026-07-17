"""Tests for the commit-bound strict-review required-check verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.automation.strict_review_artifact import (
    STRICT_REVIEW_ARTIFACT_MARKER,
    render_fenced_strict_review_artifact,
    render_strict_review_artifact,
    render_strict_review_lease,
)
from hephaestus.automation.strict_review_proof import has_trusted_strict_review_proof, main

HEAD_A = "a" * 40
HEAD_B = "b" * 40
AUTOMATION_LOGIN = "hephaestus-bot"
LEASE_ID = "test-lease"
LEASE_COMMENT_ID = 11
LEASE_EXPIRES_AT = 2_000_000_000
LEASE_TIME = "2026-01-01T00:00:00Z"
RESULT_TIME = "2026-01-01T00:01:00Z"


def _lease(head: str, *, lease_id: str = LEASE_ID, expires_at: int = LEASE_EXPIRES_AT) -> str:
    return render_strict_review_lease(head, lease_id, expires_at=expires_at)


def _artifact(
    head: str,
    *,
    is_go: bool = True,
    lease_id: str = LEASE_ID,
    lease_comment_id: int = LEASE_COMMENT_ID,
) -> str:
    verdict = "GO" if is_go else "NOGO"
    grade = "A" if is_go else "F"
    return render_fenced_strict_review_artifact(
        head,
        f"Grade: {grade}\nVerdict: {verdict}",
        is_go=is_go,
        lease_id=lease_id,
        lease_comment_id=lease_comment_id,
    )


def _comment(
    body: str,
    author: str = AUTOMATION_LOGIN,
    *,
    comment_id: int,
    created_at: str = RESULT_TIME,
) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": body,
        "created_at": created_at,
        "user": {"login": author},
    }


def _proof_comments(
    head: str, *, is_go: bool = True, lease_comment_id: int = LEASE_COMMENT_ID
) -> list[dict[str, object]]:
    return [
        _comment(_lease(head), comment_id=lease_comment_id, created_at=LEASE_TIME),
        _comment(
            _artifact(head, is_go=is_go, lease_comment_id=lease_comment_id),
            comment_id=lease_comment_id + 1,
        ),
    ]


def test_valid_trusted_current_head_go_passes() -> None:
    """A paginated trusted GO artifact authorizes its matching event SHA."""
    assert has_trusted_strict_review_proof([_proof_comments(HEAD_A)], HEAD_A, AUTOMATION_LOGIN)


@pytest.mark.parametrize(
    ("comments", "expected_head", "login"),
    [
        (_proof_comments(HEAD_A), HEAD_B, AUTOMATION_LOGIN),
        (_proof_comments(HEAD_A, is_go=False), HEAD_A, AUTOMATION_LOGIN),
        (
            [
                _comment(
                    _lease(HEAD_A),
                    "untrusted",
                    comment_id=LEASE_COMMENT_ID,
                    created_at=LEASE_TIME,
                ),
                _comment(_artifact(HEAD_A), "untrusted", comment_id=LEASE_COMMENT_ID + 1),
            ],
            HEAD_A,
            AUTOMATION_LOGIN,
        ),
        (
            [
                _comment(_lease(HEAD_A), comment_id=LEASE_COMMENT_ID, created_at=LEASE_TIME),
                _comment(
                    f"{STRICT_REVIEW_ARTIFACT_MARKER}\ninvalid",
                    comment_id=LEASE_COMMENT_ID + 1,
                ),
            ],
            HEAD_A,
            AUTOMATION_LOGIN,
        ),
        (["not a comment"], HEAD_A, AUTOMATION_LOGIN),
        (_proof_comments(HEAD_A), "not-a-sha", AUTOMATION_LOGIN),
        (_proof_comments(HEAD_A), HEAD_A, ""),
    ],
)
def test_untrusted_or_invalid_proof_fails_closed(
    comments: object, expected_head: str, login: str
) -> None:
    """Absent, malformed, stale, foreign, or invalid input never authorizes."""
    assert not has_trusted_strict_review_proof(comments, expected_head, login)


def test_latest_trusted_artifact_shadows_earlier_go() -> None:
    """A later trusted NOGO revokes an earlier trusted GO for the same head."""
    comments = [
        *_proof_comments(HEAD_A),
        _comment(_artifact(HEAD_A, is_go=False), comment_id=LEASE_COMMENT_ID + 2),
    ]

    assert not has_trusted_strict_review_proof(comments, HEAD_A, AUTOMATION_LOGIN)


def test_foreign_artifact_does_not_shadow_trusted_go() -> None:
    """Foreign lookalikes cannot revoke or replace the configured identity's proof."""
    comments = [
        *_proof_comments(HEAD_A),
        _comment(_artifact(HEAD_A, is_go=False), "foreign", comment_id=LEASE_COMMENT_ID + 2),
    ]

    assert has_trusted_strict_review_proof(comments, HEAD_A, AUTOMATION_LOGIN)


def test_synchronize_to_new_head_cannot_inherit_old_head_proof() -> None:
    """The event SHA is the authorization boundary after a PR synchronize."""
    comments = [_proof_comments(HEAD_A)]

    assert has_trusted_strict_review_proof(comments, HEAD_A, AUTOMATION_LOGIN)
    assert not has_trusted_strict_review_proof(comments, HEAD_B, AUTOMATION_LOGIN)
    comments[0].extend(_proof_comments(HEAD_B, lease_comment_id=31))
    assert has_trusted_strict_review_proof(comments, HEAD_B, AUTOMATION_LOGIN)


def test_legacy_v1_go_cannot_authorize_a_required_check() -> None:
    """The legacy unfenced grammar remains auditable but has no merge authority."""
    legacy = render_strict_review_artifact(HEAD_A, "Grade: A\nVerdict: GO", is_go=True)

    assert not has_trusted_strict_review_proof(
        [_comment(legacy, comment_id=1)], HEAD_A, AUTOMATION_LOGIN
    )


def test_legacy_v1_nogo_revokes_a_same_head_v2_go() -> None:
    """Migration compatibility preserves the adapter's fail-closed v1 revocation."""
    legacy_nogo = render_strict_review_artifact(HEAD_A, "Grade: F\nVerdict: NOGO", is_go=False)

    assert not has_trusted_strict_review_proof(
        [*_proof_comments(HEAD_A), _comment(legacy_nogo, comment_id=30)],
        HEAD_A,
        AUTOMATION_LOGIN,
    )


def test_result_posted_before_its_referenced_lease_cannot_authorize() -> None:
    """Comment IDs, not rounded timestamps, prove lease publication preceded the result."""
    result = _comment(
        _artifact(HEAD_A, lease_comment_id=20),
        comment_id=10,
        created_at=LEASE_TIME,
    )
    later_lease = _comment(_lease(HEAD_A), comment_id=20, created_at=LEASE_TIME)

    assert not has_trusted_strict_review_proof([result, later_lease], HEAD_A, AUTOMATION_LOGIN)


def test_losing_or_expired_lease_result_cannot_authorize() -> None:
    """The proof verifier independently replays election and expiry fencing."""
    winning = _comment(_lease(HEAD_A, lease_id="winner"), comment_id=10, created_at=LEASE_TIME)
    losing = _comment(_lease(HEAD_A, lease_id="loser"), comment_id=20, created_at=LEASE_TIME)
    losing_go = _comment(_artifact(HEAD_A, lease_id="loser", lease_comment_id=20), comment_id=21)
    expired = _comment(
        _lease(HEAD_A, lease_id="expired", expires_at=1), comment_id=30, created_at=LEASE_TIME
    )
    late_go = _comment(_artifact(HEAD_A, lease_id="expired", lease_comment_id=30), comment_id=31)

    assert not has_trusted_strict_review_proof(
        [winning, losing, losing_go, expired, late_go], HEAD_A, AUTOMATION_LOGIN
    )


def test_cli_returns_nonzero_when_comments_cannot_be_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Unreadable comment evidence causes the workflow-facing CLI to fail closed."""
    result = main(
        [
            "--comments-json",
            str(tmp_path / "missing.json"),
            "--expected-head-sha",
            HEAD_A,
            "--automation-login",
            AUTOMATION_LOGIN,
        ]
    )

    assert result == 1
    assert "could not read strict-review comments" in capsys.readouterr().err


def test_cli_reads_exact_head_proof(tmp_path: Path) -> None:
    """The command succeeds only when its expected SHA has a trusted GO."""
    comments = tmp_path / "comments.json"
    comments.write_text(json.dumps(_proof_comments(HEAD_A)), encoding="utf-8")

    assert (
        main(
            [
                "--comments-json",
                str(comments),
                "--expected-head-sha",
                HEAD_A,
                "--automation-login",
                AUTOMATION_LOGIN,
            ]
        )
        == 0
    )
