"""Tests for lifecycle-backed alert transition tracking."""

from __future__ import annotations

import importlib

import pytest


def test_alert_tracker_emits_one_fire_and_one_resolution() -> None:
    """Persistent degradation produces no repeated alert-event spam."""
    alerts = importlib.import_module("hephaestus.observability.alerts")
    tracker = alerts.AlertTracker(queue_depth_threshold=50)
    unhealthy = {
        "queue_depths": {"planning": 3},
        "queue_capacities": {"planning": 4},
        "circuit_breakers": {"github": {"state": "open"}},
    }

    fired = tracker.observe(unhealthy)
    repeated = tracker.observe(unhealthy)
    resolved = tracker.observe(
        {
            "queue_depths": {"planning": 0},
            "queue_capacities": {"planning": 4},
            "circuit_breakers": {},
        }
    )

    assert {(event.name, event.status) for event in fired} == {
        ("circuit_breaker_open", "fired"),
        ("queue_depth_exceeds", "fired"),
    }
    assert repeated == []
    assert {(event.name, event.status) for event in resolved} == {
        ("circuit_breaker_open", "resolved"),
        ("queue_depth_exceeds", "resolved"),
    }


def test_queue_depth_threshold_is_a_percentage_of_capacity() -> None:
    """One depth value fires or stays quiet depending only on its capacity."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    def names(depth: int, capacity: int, threshold: int) -> list[str]:
        events = alerts.evaluate_alerts(
            {"queue_depths": {"planning": depth}, "queue_capacities": {"planning": capacity}},
            queue_depth_threshold=threshold,
        )
        return [event.name for event in events]

    assert names(10, 10, 100) == ["queue_depth_exceeds"]  # filled capacity
    assert names(9, 10, 100) == []  # one item short of full
    assert names(9, 10, 90) == ["queue_depth_exceeds"]  # exactly 90% of capacity
    assert names(9, 10, 91) == []  # just above the observed share
    assert names(5, 10, 50) == ["queue_depth_exceeds"]  # exactly at the threshold
    assert names(4, 10, 50) == []  # below the threshold
    assert names(100, 10_000, 5) == []  # a large absolute depth is still a small share


def test_zero_threshold_fires_on_any_ready_backlog() -> None:
    """Threshold ``0`` keeps the historical any-nonempty-queue rule."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    fired = alerts.evaluate_alerts(
        {"queue_depths": {"planning": 1}, "queue_capacities": {"planning": 100}},
        queue_depth_threshold=0,
    )
    quiet = alerts.evaluate_alerts(
        {"queue_depths": {"planning": 0}, "queue_capacities": {"planning": 100}},
        queue_depth_threshold=0,
    )

    assert [event.name for event in fired] == ["queue_depth_exceeds"]
    assert quiet == []


def test_depth_rule_skips_stages_without_a_valid_capacity() -> None:
    """A missing or unusable capacity produces no speculative depth alert."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    assert alarms_for(alerts, {"queue_depths": {"planning": 5}}) == []
    assert alarms_for(alerts, {"queue_depths": {"planning": 5}, "queue_capacities": {}}) == []
    assert (
        alarms_for(
            alerts,
            {
                "queue_depths": {"planning": 5},
                "queue_capacities": {"planning": 0},
            },
        )
        == []
    )
    assert (
        alarms_for(
            alerts,
            {
                "queue_depths": {"planning": 5},
                "queue_capacities": {"planning": True},
            },
        )
        == []
    )


def test_bool_depth_is_not_a_backlog() -> None:
    """``True`` is an int subclass and must not count as a queued item."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    assert (
        alarms_for(
            alerts,
            {"queue_depths": {"planning": True}, "queue_capacities": {"planning": 1}},
            threshold=0,
        )
        == []
    )


def test_queue_saturated_fires_critical_and_resolves() -> None:
    """Queues at capacity raise one critical alert that clears when they drain."""
    alerts = importlib.import_module("hephaestus.observability.alerts")
    tracker = alerts.AlertTracker()

    fired = tracker.observe(
        {"queue_depths": {}, "saturated_queues": ["implementation", "completion"]}
    )
    repeated = tracker.observe(
        {"queue_depths": {}, "saturated_queues": ["implementation", "completion"]}
    )
    resolved = tracker.observe({"queue_depths": {}, "saturated_queues": []})

    assert [(event.name, event.severity, event.status) for event in fired] == [
        ("queue_saturated", "critical", "fired")
    ]
    assert fired[0].message == "queues at capacity: completion, implementation"
    assert repeated == []
    assert [(event.name, event.status) for event in resolved] == [("queue_saturated", "resolved")]


def test_saturated_queues_ignores_a_bare_string() -> None:
    """A scalar value is not a queue list and must not fire the rule."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    assert alerts.evaluate_alerts({"saturated_queues": "implementation"}) == []


def test_out_of_range_queue_depth_threshold_rejected() -> None:
    """Both entry points reject a percentage above 100."""
    alerts = importlib.import_module("hephaestus.observability.alerts")

    for rejected in (-1, 101):
        with pytest.raises(ValueError):
            alerts.evaluate_alerts({}, queue_depth_threshold=rejected)
        with pytest.raises(ValueError):
            alerts.AlertTracker(queue_depth_threshold=rejected)


def alarms_for(alerts: object, snapshot: dict[str, object], *, threshold: int = 100) -> list[str]:
    """Return the alert names one snapshot raises at a percentage threshold."""
    events = alerts.evaluate_alerts(snapshot, queue_depth_threshold=threshold)  # type: ignore[attr-defined]
    return [event.name for event in events]


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
