"""Regression tests for #2055's authenticated strict-review proof."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import hephaestus.automation.pipeline_github as pg

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

    def capture(pr_number: int, marker_prefix: str, body: str) -> bool:
        assert pr_number == 71
        published["marker"] = marker_prefix
        published["body"] = body
        return True

    monkeypatch.setattr(adapter, "upsert_pr_comment", capture)
    adapter.publish_strict_review_artifact(71, _HEAD_SHA, verdict_body)
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
