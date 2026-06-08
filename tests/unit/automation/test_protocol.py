"""Tests for the automation protocol-string constants.

Pins the exact wire-protocol marker values and verifies that the historical
import paths (``models.PLAN_COMMENT_MARKER``, ``review_state.PLAN_REVIEW_PREFIX``)
re-export the same object as the canonical :mod:`hephaestus.automation.protocol`.
"""

from __future__ import annotations

from hephaestus.automation import models, protocol, review_state


class TestProtocolConstants:
    """Tests for the canonical protocol-string constants."""

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


class TestShimReExports:
    """The pre-refactor import paths must keep working without copying."""

    def test_models_re_exports_plan_comment_marker(self) -> None:
        assert models.PLAN_COMMENT_MARKER == protocol.PLAN_COMMENT_MARKER

    def test_review_state_re_exports_plan_review_prefix(self) -> None:
        assert review_state.PLAN_REVIEW_PREFIX == protocol.PLAN_REVIEW_PREFIX
