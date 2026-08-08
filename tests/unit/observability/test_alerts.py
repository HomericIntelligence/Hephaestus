"""Tests for lifecycle-backed alert transition tracking."""

from __future__ import annotations

import importlib

import pytest


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


def test_pipeline_stalled_fires_at_threshold() -> None:
    """A snapshot at the stall threshold yields one warning-severity event."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    events = alerts.evaluate_alerts({"stalled_ticks": 3}, stalled_ticks_threshold=3)

    assert [(event.name, event.severity, event.status) for event in events] == [
        ("pipeline_stalled", "warning", "fired")
    ]


def test_pipeline_stalled_quiet_below_threshold_and_for_bool() -> None:
    """Below-threshold counts and bool values never fire the stall rule."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    assert alerts.evaluate_alerts({"stalled_ticks": 2}, stalled_ticks_threshold=3) == []
    # ``True`` is an int subclass; it must not be treated as a stall count.
    assert alerts.evaluate_alerts({"stalled_ticks": True}, stalled_ticks_threshold=1) == []


def test_pipeline_stalled_resolves_via_tracker() -> None:
    """The tracker turns a stall condition into fire then resolve transitions."""
    alerts = importlib.import_module("hephaestus.observability.alerts")
    tracker = alerts.AlertTracker(stalled_ticks_threshold=3)

    fired = tracker.observe({"stalled_ticks": 4})
    resolved = tracker.observe({"stalled_ticks": 0})

    assert {(event.name, event.status) for event in fired} == {("pipeline_stalled", "fired")}
    assert {(event.name, event.status) for event in resolved} == {("pipeline_stalled", "resolved")}


def test_negative_stalled_threshold_rejected() -> None:
    """A negative stall threshold is rejected in both the function and tracker."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    with pytest.raises(ValueError):
        alerts.evaluate_alerts({}, stalled_ticks_threshold=-1)
    with pytest.raises(ValueError):
        alerts.AlertTracker(stalled_ticks_threshold=-1)
