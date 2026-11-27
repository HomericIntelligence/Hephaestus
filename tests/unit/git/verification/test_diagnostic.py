"""Tests for the shared host-verification diagnostic."""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest

from hephaestus.git.verification.diagnostic import (
    host_verification_failure_comment,
    publish_host_verification_failure,
    publish_stage_diagnostic,
)
from hephaestus.git.verification.specs import PRE_PR_TEST


class _Recorder:
    """Record marker comments a caller publishes."""

    def __init__(self, error: Exception | None = None) -> None:
        """Set the failure the next write raises, if any."""
        self.calls: list[tuple[int, str, str]] = []
        self._error = error

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Record one write, or raise the scripted error."""
        self.calls.append((issue_number, marker, body))
        if self._error is not None:
            raise self._error


def test_failure_comment_names_the_exact_head_and_command() -> None:
    """The marker and body bind one head to the fixed check."""
    marker, body = host_verification_failure_comment(
        cast(Any, PRE_PR_TEST),
        {"head_sha": "abc123", "argv": PRE_PR_TEST.argv, "failure_kind": "timeout"},
    )

    assert marker.endswith("abc123:implementation_pre_pr_test -->")
    assert "**Reviewed head**" in body
    assert "uv run pytest tests -q --tb=short" in body
    assert "**Failure classification**" in body
    assert "    timeout" in body


def test_failure_comment_redacts_a_token_and_adds_optional_fields() -> None:
    """Untrusted diagnostic text is redacted and optional fields are inert."""
    token = "ghp_" + "a" * 36
    _, body = host_verification_failure_comment(
        None,
        {
            "head_sha": "abc123",
            "operation": "rebase",
            "path": "hephaestus/git/verification/specs.py",
            "receipt_id": "receipt-1",
            "error": f"failed with {token}",
        },
    )

    assert "**Source head**" in body
    assert "**Affected path**" in body
    assert "**Receipt**" in body
    assert token not in body
    assert "<redacted>" in body


def test_publish_returns_failure_without_raising() -> None:
    """A GitHub write failure reaches the caller as a boolean."""
    github = _Recorder(RuntimeError("write failed"))

    published = publish_host_verification_failure(
        github, 7, cast(Any, PRE_PR_TEST), {"head_sha": "abc123"}, logging.getLogger("test")
    )

    assert published is False
    assert len(github.calls) == 1


def test_publish_stage_diagnostic_forwards_and_propagates() -> None:
    """The stage helper writes the marker and keeps its caller's error route."""
    github = _Recorder()

    publish_stage_diagnostic(github, 17, "<!-- marker -->", "body text")

    assert github.calls == [(17, "<!-- marker -->", "body text")]

    failing = _Recorder(RuntimeError("comment write failed"))
    with pytest.raises(RuntimeError, match="comment write failed"):
        publish_stage_diagnostic(failing, 17, "<!-- marker -->", "body text")
