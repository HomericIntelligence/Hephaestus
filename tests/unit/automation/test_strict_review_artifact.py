"""Regressions for the immutable, fenced #2055 strict-review proof."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import hephaestus.automation.pipeline_github as pg
from hephaestus.automation.pipeline.stages import StrictReviewArtifact, StrictReviewLease
from hephaestus.automation.strict_review_artifact import (
    STRICT_REVIEW_ARTIFACT_V2_MARKER,
    STRICT_REVIEW_LEASE_MARKER,
    parse_strict_review_artifact,
    parse_strict_review_lease,
    render_fenced_strict_review_artifact,
    render_strict_review_artifact,
    render_strict_review_lease,
)

_HEAD_SHA = "a" * 40
_NEXT_HEAD_SHA = "b" * 40
_AUTOMATION_LOGIN = "hephaestus-bot"
_LEASE_ID = "lease-123"
_LEASE_COMMENT_ID = 11
_LEASE_EXPIRES_AT = 1_700_000_000
_LEASE_CREATED_AT = "2023-11-14T22:13:19Z"
_RESULT_CREATED_AT = "2023-11-14T22:13:20Z"


def _comment(
    identifier: int,
    body: str,
    *,
    author: str = _AUTOMATION_LOGIN,
    created_at: str = _RESULT_CREATED_AT,
) -> dict[str, object]:
    return {
        "id": identifier,
        "databaseId": identifier,
        "body": body,
        "created_at": created_at,
        "user": {"login": author},
    }


def _lease(
    *, head: str = _HEAD_SHA, lease_id: str = _LEASE_ID, comment_id: int = _LEASE_COMMENT_ID
) -> dict[str, object]:
    return _comment(
        comment_id,
        render_strict_review_lease(head, lease_id, expires_at=_LEASE_EXPIRES_AT),
        created_at=_LEASE_CREATED_AT,
    )


def _artifact(
    *,
    is_go: bool = True,
    head: str = _HEAD_SHA,
    lease_id: str = _LEASE_ID,
    lease_comment_id: int = _LEASE_COMMENT_ID,
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


def _read_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comments: list[dict[str, object]],
    head: str = _HEAD_SHA,
) -> StrictReviewArtifact | None:
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([comments]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    return adapter.strict_review_artifact(71, head)


def _read_terminal_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    comments: list[dict[str, object]],
    head: str = _HEAD_SHA,
) -> StrictReviewArtifact | None:
    """Read the recovery-only terminal-result channel from canned comments."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([comments]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    return adapter.strict_review_terminal_artifact(71, head)


def test_lease_round_trip_binds_one_exact_head_and_fencing_token() -> None:
    """Lease bytes are immutable, digest-checked evidence for one review generation."""
    lease = render_strict_review_lease(_HEAD_SHA, _LEASE_ID, expires_at=_LEASE_EXPIRES_AT)

    assert lease.startswith(STRICT_REVIEW_LEASE_MARKER)
    parsed = parse_strict_review_lease(lease)
    assert parsed is not None
    assert parsed.head_sha == _HEAD_SHA
    assert parsed.lease_id == _LEASE_ID
    assert parsed.expires_at == _LEASE_EXPIRES_AT


def test_elected_fenced_go_authorizes_only_its_exact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A v2 GO must point at the elected immutable lease before it can authorize."""
    proof = _artifact()

    authorized = _read_artifact(tmp_path, monkeypatch, [_lease(), _comment(12, proof)], _HEAD_SHA)

    assert authorized is not None
    assert authorized.is_go is True
    assert authorized.head_sha == _HEAD_SHA
    assert (
        _read_artifact(tmp_path, monkeypatch, [_lease(), _comment(12, proof)], _NEXT_HEAD_SHA)
        is None
    )


def test_legacy_v1_go_cannot_authorize_a_pipeline_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unfenced historical GO is audit evidence, never current merge authority."""
    legacy = render_strict_review_artifact(_HEAD_SHA, "Grade: A\nVerdict: GO", is_go=True)

    assert parse_strict_review_artifact(legacy) is not None
    assert _read_artifact(tmp_path, monkeypatch, [_comment(12, legacy)]) is None


def test_terminal_nogo_dominates_any_stale_go_for_the_same_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No later stale GO can replace the durable NOGO generation result."""
    comments = [
        _lease(),
        _comment(12, _artifact(is_go=False)),
        _comment(13, _artifact(is_go=True)),
    ]

    assert _read_artifact(tmp_path, monkeypatch, comments) is None


def test_terminal_nogo_is_observable_for_restart_recovery_but_not_merge_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A final NOGO is distinct from a live lease without becoming a GO proof."""
    comments = [_lease(), _comment(12, _artifact(is_go=False))]

    terminal = _read_terminal_artifact(tmp_path, monkeypatch, comments)

    assert terminal is not None
    assert terminal.is_go is False
    assert terminal.head_sha == _HEAD_SHA
    assert terminal.verdict == "NOGO"
    assert terminal.verdict_body.endswith("Grade: F\nVerdict: NOGO")
    assert terminal.schema_version == 2
    assert _read_artifact(tmp_path, monkeypatch, comments) is None


def test_foreign_or_losing_lease_artifact_never_authorizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A result linked to a later competing lease cannot win the election."""
    later_lease = _comment(
        20,
        render_strict_review_lease(_HEAD_SHA, "later-lease", expires_at=_LEASE_EXPIRES_AT),
        created_at=_LEASE_CREATED_AT,
    )
    losing_go = _artifact(lease_id="later-lease", lease_comment_id=20)
    foreign_go = _comment(12, _artifact(), author="mallory")

    assert (
        _read_artifact(tmp_path, monkeypatch, [_lease(), later_lease, _comment(21, losing_go)])
        is None
    )
    assert _read_artifact(tmp_path, monkeypatch, [_lease(), foreign_go]) is None


def test_expired_unfinalized_lease_can_be_recovered_but_its_late_output_is_fenced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease expiry restores liveness without accepting a crashed holder's late GO."""
    expired = _comment(
        11,
        render_strict_review_lease(_HEAD_SHA, _LEASE_ID, expires_at=1_600_000_000),
        created_at="2020-09-13T12:26:39Z",
    )
    recovered = _comment(
        20,
        render_strict_review_lease(_HEAD_SHA, "recovered", expires_at=_LEASE_EXPIRES_AT),
        created_at=_LEASE_CREATED_AT,
    )
    late_old = _comment(21, _artifact(lease_id=_LEASE_ID, lease_comment_id=11))
    recovered_go = _comment(22, _artifact(lease_id="recovered", lease_comment_id=20))

    assert _read_artifact(tmp_path, monkeypatch, [expired, recovered, late_old]) is None
    assert _read_artifact(tmp_path, monkeypatch, [expired, recovered, recovered_go]) is not None


def test_claim_appends_and_elects_one_immutable_lease_before_review_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real adapter persists a lease and uses its returned comment id as the fence."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    calls: list[list[str]] = []
    created: dict[str, object] | None = None

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal created
        calls.append(argv)
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if "POST" in argv:
            body_arg = next(arg for arg in argv if arg.startswith("body=@"))
            created = _comment(
                41,
                Path(body_arg.removeprefix("body=@")).read_text(),
                created_at=_LEASE_CREATED_AT,
            )
            return SimpleNamespace(stdout=json.dumps({"id": 41}))
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([[created] if created is not None else []]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    monkeypatch.setattr(time, "time", lambda: 1_699_999_000)

    lease = adapter.claim_strict_review_lease(71, _HEAD_SHA)

    assert lease is not None
    assert lease.head_sha == _HEAD_SHA
    assert lease.comment_id == 41
    assert lease.lease_id
    assert any("POST" in call for call in calls)


def test_live_elected_lease_blocks_a_second_reviewer_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later coordinator sees the existing election and must not append another lease."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    calls: list[list[str]] = []
    winner = _lease()

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([[winner]]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    monkeypatch.setattr(time, "time", lambda: 1_699_999_000)

    assert adapter.claim_strict_review_lease(71, _HEAD_SHA) is None
    assert not any("POST" in call for call in calls)


def test_claim_refetches_after_post_and_yields_to_a_lower_comment_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent earlier claim seen after POST must prevent duplicate dispatch."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    calls: list[list[str]] = []
    posted = False
    winner = _lease(lease_id="winner", comment_id=40)

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        nonlocal posted
        calls.append(argv)
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if "POST" in argv:
            posted = True
            return SimpleNamespace(stdout=json.dumps({"id": 41}))
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([[winner] if posted else []]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    monkeypatch.setattr(time, "time", lambda: 1_699_999_000)

    assert adapter.claim_strict_review_lease(71, _HEAD_SHA) is None
    assert any("POST" in call for call in calls)


def test_publish_rejects_a_losing_fence_before_writing_a_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapter refuses to append a result for a lease another coordinator won."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    calls: list[list[str]] = []
    winner = _lease()

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([[winner]]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    monkeypatch.setattr(time, "time", lambda: 1_699_999_000)

    assert not adapter.publish_strict_review_artifact(
        71,
        _HEAD_SHA,
        "Grade: A\nVerdict: GO",
        is_go=True,
        lease=StrictReviewLease(_HEAD_SHA, "loser", 20),
    )
    assert not any("POST" in call for call in calls)
    assert not any(STRICT_REVIEW_ARTIFACT_V2_MARKER in " ".join(call) for call in calls)
