"""Tests for the stage diagnostic publication helper."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.stages.pr_review_diagnostics import (
    publish_stage_diagnostic,
)


class _Recorder:
    """Record the comment writes a caller makes."""

    def __init__(self, error: Exception | None = None) -> None:
        """Set the failure the next write raises, if any."""
        self.calls: list[tuple[int, str, str]] = []
        self._error = error

    def upsert_issue_comment(self, issue_number: int, marker: str, body: str) -> None:
        """Record one write, or raise the scripted error."""
        self.calls.append((issue_number, marker, body))
        if self._error is not None:
            raise self._error


def test_publish_stage_diagnostic_writes_the_marker_comment() -> None:
    """The helper forwards the issue, marker, and body unchanged."""
    github = _Recorder()

    publish_stage_diagnostic(github, 17, "<!-- marker -->", "body text")

    assert github.calls == [(17, "<!-- marker -->", "body text")]


def test_publish_stage_diagnostic_propagates_the_failure() -> None:
    """A write failure reaches the caller for its own recovery route."""
    error = RuntimeError("comment write failed")
    github = _Recorder(error)

    with pytest.raises(RuntimeError, match="comment write failed"):
        publish_stage_diagnostic(github, 17, "<!-- marker -->", "body text")
    assert len(github.calls) == 1
