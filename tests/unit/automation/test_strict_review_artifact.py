"""Regression tests for #2055's authenticated strict-review proof."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hephaestus.automation.pipeline_github as pg
from hephaestus.automation.strict_review_artifact import (
    MAX_ARTIFACT_BYTES,
    STRICT_REVIEW_ARTIFACT_MARKER,
    parse_strict_review_artifact,
    render_strict_review_artifact,
)

_HEAD_SHA = "a" * 40
_AUTOMATION_LOGIN = "hephaestus-bot"


def _published_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdict_body: str = "Grade: A\nVerdict: GO",
) -> tuple[pg.PipelineGitHub, dict[str, str]]:
    """Publish a proof while retaining its exact emitted bytes for a read-back."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    published: dict[str, str] = {}

    def capture(pr_number: int, body: str, login: str) -> None:
        assert pr_number == 71
        published["body"] = body
        assert login == _AUTOMATION_LOGIN

    monkeypatch.setattr(adapter, "_strict_review_login", lambda: _AUTOMATION_LOGIN)
    monkeypatch.setattr(adapter, "_upsert_owned_strict_review_artifact", capture)
    adapter.publish_strict_review_artifact(
        71, _HEAD_SHA, verdict_body, is_go="Verdict: GO" in verdict_body
    )
    return adapter, published


def _read_artifact(
    adapter: pg.PipelineGitHub,
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    author: str = _AUTOMATION_LOGIN,
) -> Any:
    """Read one REST comment as the configured automation identity."""

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(stdout=json.dumps([[{"body": body, "user": {"login": author}}]]))
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    return adapter.strict_review_artifact(71, _HEAD_SHA)


def _digest_valid_artifact(*, header_verdict: str, verdict_body: str) -> str:
    """Build a syntactically ordered, digest-valid artifact for parser negatives."""
    digest = hashlib.sha256(f"{_HEAD_SHA}\n{header_verdict}\n{verdict_body}".encode()).hexdigest()
    return (
        f"{STRICT_REVIEW_ARTIFACT_MARKER}\n"
        f"Head-SHA: {_HEAD_SHA}\n"
        f"Digest: {digest}\n"
        f"Verdict: {header_verdict}\n\n"
        f"{verdict_body}"
    )


def test_valid_automation_artifact_round_trips_for_its_exact_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A published GO proof authorizes only after authenticated read-back."""
    adapter, published = _published_artifact(tmp_path, monkeypatch)

    artifact = _read_artifact(adapter, monkeypatch, body=published["body"])

    assert artifact is not None
    assert artifact.is_go is True
    assert artifact.head_sha == _HEAD_SHA
    assert artifact.verdict == "GO"


def test_byte_tampered_artifact_never_authorizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One changed byte must invalidate the digest/grammar-bound proof."""
    adapter, published = _published_artifact(tmp_path, monkeypatch)

    assert _read_artifact(adapter, monkeypatch, body=published["body"] + "\n") is None


def test_foreign_author_cannot_replay_a_valid_automation_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proof bytes alone are insufficient without the authenticated author."""
    adapter, published = _published_artifact(tmp_path, monkeypatch)

    assert _read_artifact(adapter, monkeypatch, body=published["body"], author="mallory") is None


def test_valid_nogo_artifact_never_authorizes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A syntactically valid NOGO proof cannot be mistaken for merge consent."""
    adapter, published = _published_artifact(
        tmp_path,
        monkeypatch,
        verdict_body="Grade: F\nVerdict: NOGO",
    )

    assert _read_artifact(adapter, monkeypatch, body=published["body"]) is None


@pytest.mark.parametrize(
    ("header_verdict", "verdict_body"),
    [
        ("GO", "The review looks good but omitted its final contract."),
        ("GO", "Grade: F\nVerdict: NOGO"),
        ("NOGO", "Grade: A\nVerdict: GO"),
    ],
)
def test_digest_valid_artifact_still_requires_matching_final_machine_verdict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    header_verdict: str,
    verdict_body: str,
) -> None:
    """Digest/authorship cannot elevate a malformed or contradictory reviewer body."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    malformed = _digest_valid_artifact(
        header_verdict=header_verdict,
        verdict_body=verdict_body,
    )

    assert parse_strict_review_artifact(malformed) is None
    assert _read_artifact(adapter, monkeypatch, body=malformed) is None


def test_render_rejects_a_body_without_matching_final_machine_verdict() -> None:
    """The producer cannot create an artifact the strict parser would reject."""
    with pytest.raises(ValueError, match="matching Grade/Verdict"):
        render_strict_review_artifact(_HEAD_SHA, "Grade: F\nVerdict: NOGO", is_go=True)


def test_foreign_marker_does_not_block_automation_artifact_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Publishing creates our comment instead of PATCHing/deleting a foreign marker."""
    adapter = pg.PipelineGitHub("org", repo="repo-a", repo_root=tmp_path)
    calls: list[list[str]] = []

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        [
                            {
                                "id": 99,
                                "body": "<!-- hephaestus-strict-review: v1 -->\nforeign",
                                "user": {"login": "mallory"},
                            }
                        ]
                    ]
                )
            )
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    adapter.publish_strict_review_artifact(71, _HEAD_SHA, "Grade: A\nVerdict: GO", is_go=True)

    assert any(call[:3] == ["issue", "comment", "71"] for call in calls)
    assert not any(call[:3] == ["api", "--method", "PATCH"] for call in calls)
    assert not any(call[:3] == ["api", "--method", "DELETE"] for call in calls)


def test_oversized_or_malformed_artifact_never_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The artifact grammar rejects byte-limit and header/digest violations."""
    adapter, published = _published_artifact(tmp_path, monkeypatch)
    assert _read_artifact(adapter, monkeypatch, body="x" * (MAX_ARTIFACT_BYTES + 1)) is None
    malformed = published["body"].replace("Digest: ", "Digest: bad")
    assert parse_strict_review_artifact(malformed) is None


def test_stale_head_and_latest_nogo_page_never_authorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A proof applies only to its SHA and the newest owned marker wins."""
    adapter, go = _published_artifact(tmp_path, monkeypatch)
    _, nogo = _published_artifact(
        tmp_path,
        monkeypatch,
        verdict_body="Grade: F\nVerdict: NOGO",
    )

    def fake_gh_call(argv: list[str], **_kwargs: object) -> SimpleNamespace:
        if argv[:3] == ["api", "user", "--jq"]:
            return SimpleNamespace(stdout=f"{_AUTOMATION_LOGIN}\n")
        if argv[:2] == ["api", "/repos/org/repo-a/issues/71/comments"]:
            return SimpleNamespace(
                stdout=json.dumps(
                    [
                        [{"body": go["body"], "user": {"login": _AUTOMATION_LOGIN}}],
                        [{"body": nogo["body"], "user": {"login": _AUTOMATION_LOGIN}}],
                    ]
                )
            )
        raise AssertionError(f"unexpected gh invocation: {argv!r}")

    monkeypatch.setattr(pg, "gh_call", fake_gh_call)
    assert adapter.strict_review_artifact(71, _HEAD_SHA) is None
    assert adapter.strict_review_artifact(71, "b" * 40) is None
