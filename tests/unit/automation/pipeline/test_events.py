"""Tests for the intentionally empty stage-event surface."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.events import encode_stage_event


def test_legacy_zero_thread_event_contract_is_removed() -> None:
    """A textual zero-thread anomaly cannot become a durable stage event."""
    with pytest.raises(TypeError, match="unsupported stage event"):
        encode_stage_event(object())
