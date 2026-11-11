"""Behaviour of the stage-neutral host-verification diagnostic (issue #3521)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pytest

from hephaestus.automation.pipeline import host_verification_diagnostic as diagnostic

_HEAD = "a" * 40


@dataclass
class _RecordedComment:
    issue_number: int
    marker: str
    body: str


class _Writer:
    """Record every upsert call, or raise the configured error."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[_RecordedComment] = []
        self._error = error

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        if self._error is not None:
            raise self._error
        self.calls.append(_RecordedComment(issue_number, marker, body))


@dataclass
class _Verification:
    descr: str
    argv: tuple[str, ...]
    changed_path: str | None = None


def test_publish_writes_one_marker_owned_comment() -> None:
    """A successful publish writes exactly one comment with the exact-head marker."""
    writer = _Writer()

    published = diagnostic.publish_host_verification_failure(
        writer,
        7,
        _Verification("pytest", ("uv", "run", "pytest")),
        {"head_sha": _HEAD},
        logging.getLogger(__name__),
    )

    assert published is True
    assert len(writer.calls) == 1
    call = writer.calls[0]
    assert call.issue_number == 7
    assert call.marker.startswith("<!-- hephaestus-host-verification-failure:")
    assert call.marker.endswith("-->")
    assert call.marker.endswith(f"{_HEAD}:pytest -->")


def test_publish_returns_false_and_warns_when_the_writer_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed comment publish is reported, never raised to the caller."""
    writer = _Writer(error=RuntimeError("boom"))

    with caplog.at_level(logging.WARNING):
        published = diagnostic.publish_host_verification_failure(
            writer, 7, _Verification("pytest", ()), {"head_sha": _HEAD}, logging.getLogger("test")
        )

    assert published is False
    assert "failed to publish PR #7 host-verification failure: boom" in caplog.text


def test_rebase_shape_names_the_source_head() -> None:
    """A blocked rebase reports the operation and the source head."""
    marker, body = diagnostic.host_verification_failure_comment(
        None, {"operation": "rebase", "head_sha": _HEAD}
    )

    assert f"{_HEAD}:rebase -->" in marker
    assert "A required rebase host operation is blocked." in body
    assert "**Source head**" in body


def test_capability_shape_reports_an_unavailable_capability() -> None:
    """A capability failure says that the fixed check did not start."""
    _marker, body = diagnostic.host_verification_failure_comment(
        None, {"capability_failure": True, "head_sha": _HEAD}
    )

    assert "A required host capability is unavailable." in body


def test_oversize_diagnostic_is_bounded() -> None:
    """A long failure text is cut to the transport cap."""
    _marker, body = diagnostic.host_verification_failure_comment(
        None, {"head_sha": _HEAD, "error": "x" * 8_000}
    )

    failure = body.split("**Failure**\n\n", 1)[1]
    assert len(failure.strip()) == diagnostic.HOST_VERIFICATION_DIAGNOSTIC_MAX
    # The tail is kept, so the cut keeps the most recent text.
    assert "x" * diagnostic.HOST_VERIFICATION_DIAGNOSTIC_MAX in body
