"""Tests for lifecycle-backed alert transition tracking."""

from __future__ import annotations

import importlib


def test_alert_tracker_emits_one_fire_and_one_resolution() -> None:
    """Persistent degradation produces no repeated alert-event spam."""
    alerts = importlib.import_module("hephaestus.observability.alerts")
    tracker = alerts.AlertTracker(queue_depth_threshold=2)
    unhealthy = {
        "queue_depths": {"planning": 3},
        "circuit_breakers": {"github": {"state": "open"}},
    }

    fired = tracker.observe(unhealthy)
    repeated = tracker.observe(unhealthy)
    resolved = tracker.observe({"queue_depths": {"planning": 0}, "circuit_breakers": {}})

    assert {(event.name, event.status) for event in fired} == {
        ("circuit_breaker_open", "fired"),
        ("queue_depth_exceeds", "fired"),
    }
    assert repeated == []
    assert {(event.name, event.status) for event in resolved} == {
        ("circuit_breaker_open", "resolved"),
        ("queue_depth_exceeds", "resolved"),
    }
