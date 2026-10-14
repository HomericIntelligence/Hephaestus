"""Test the current automation protocol markers and display headings."""

from __future__ import annotations

import pytest

from hephaestus.automation import protocol


class TestProtocolConstants:
    """Tests for the canonical protocol-string constants."""

    def test_shared_planning_markers_use_the_homeric_intelligence_namespace(self) -> None:
        """New plan artifacts use the marker Athena resolves on shared issues."""
        assert protocol.PLAN_CANONICAL_MARKER == "<!-- HomericIntelligence:plan-issue -->"
        assert protocol.PLAN_REVIEW_CANONICAL_MARKER == "<!-- HomericIntelligence:issue-review -->"

    def test_plan_comment_marker_value(self) -> None:
        assert protocol.PLAN_COMMENT_MARKER == "# Implementation Plan"

    def test_plan_review_prefix_value(self) -> None:
        assert protocol.PLAN_REVIEW_PREFIX == "## 🔍 Plan Review"

    def test_markers_are_strings(self) -> None:
        assert isinstance(protocol.PLAN_COMMENT_MARKER, str)
        assert isinstance(protocol.PLAN_REVIEW_PREFIX, str)

    def test_markers_are_non_empty(self) -> None:
        assert protocol.PLAN_COMMENT_MARKER
        assert protocol.PLAN_REVIEW_PREFIX


def test_retired_reviewer_protocol_is_unavailable() -> None:
    """Queue stages do not expose the removed standalone reviewer interface."""
    removed_name = "ReviewerProtocol"
    assert removed_name not in protocol.__all__
    with pytest.raises(AttributeError):
        getattr(protocol, removed_name)
