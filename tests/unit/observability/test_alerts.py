"""Unit tests for hephaestus.observability.alerts."""

from __future__ import annotations

import pytest

from hephaestus.observability.alerts import (
    DEFAULT_RULES,
    AlertEvent,
    AlertRule,
    evaluate_alerts,
)


class TestCircuitBreakerOpenRule:
    """Tests for the circuit_breaker_open rule."""

    def test_fires_when_any_breaker_open(self) -> None:
        snapshot = {
            "circuit_breakers": {
                "github-api": {"state": "closed"},
                "nats-subscriber": {"state": "open"},
            }
        }
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "circuit_breaker_open" in names

    def test_does_not_fire_when_all_closed(self) -> None:
        snapshot = {
            "circuit_breakers": {
                "github-api": {"state": "closed"},
                "nats-subscriber": {"state": "half_open"},
            }
        }
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "circuit_breaker_open" not in names

    def test_missing_key_does_not_fire(self) -> None:
        events = evaluate_alerts({})
        names = [e.rule_name for e in events]
        assert "circuit_breaker_open" not in names

    def test_fired_event_severity_and_message(self) -> None:
        snapshot = {"circuit_breakers": {"x": {"state": "open"}}}
        events = evaluate_alerts(snapshot)
        event = next(e for e in events if e.rule_name == "circuit_breaker_open")
        assert event.severity == "critical"
        assert "OPEN" in event.message
        assert event.snapshot is snapshot


class TestQueueDepthExceedsRule:
    """Tests for the queue_depth_exceeds rule."""

    def test_fires_when_depth_exceeds_default_threshold(self) -> None:
        snapshot = {"queue_depths": {"plan": 150}}
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "queue_depth_exceeds" in names

    def test_does_not_fire_when_under_threshold(self) -> None:
        snapshot = {"queue_depths": {"plan": 5}}
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "queue_depth_exceeds" not in names

    def test_respects_custom_threshold(self) -> None:
        snapshot = {"queue_depths": {"plan": 10}, "queue_depth_threshold": 5}
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "queue_depth_exceeds" in names

    def test_severity_is_warning(self) -> None:
        snapshot = {"queue_depths": {"plan": 200}}
        events = evaluate_alerts(snapshot)
        event = next(e for e in events if e.rule_name == "queue_depth_exceeds")
        assert event.severity == "warning"


class TestSubscriberStalledRule:
    """Tests for the subscriber_stalled rule."""

    def test_fires_when_state_is_error(self) -> None:
        snapshot = {"subscriber_health": {"state": "error"}}
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "subscriber_stalled" in names

    def test_does_not_fire_when_state_is_connected(self) -> None:
        snapshot = {"subscriber_health": {"state": "connected"}}
        events = evaluate_alerts(snapshot)
        names = [e.rule_name for e in events]
        assert "subscriber_stalled" not in names

    def test_missing_health_does_not_fire(self) -> None:
        events = evaluate_alerts({})
        names = [e.rule_name for e in events]
        assert "subscriber_stalled" not in names

    def test_severity_is_critical(self) -> None:
        snapshot = {"subscriber_health": {"state": "error"}}
        events = evaluate_alerts(snapshot)
        event = next(e for e in events if e.rule_name == "subscriber_stalled")
        assert event.severity == "critical"


class TestEvaluateAlerts:
    """Tests for evaluate_alerts() overall behavior."""

    def test_no_rules_fire_on_healthy_snapshot(self) -> None:
        snapshot = {
            "circuit_breakers": {"x": {"state": "closed"}},
            "queue_depths": {"plan": 1},
            "subscriber_health": {"state": "connected"},
        }
        assert evaluate_alerts(snapshot) == []

    def test_multiple_rules_can_fire_together(self) -> None:
        snapshot = {
            "circuit_breakers": {"x": {"state": "open"}},
            "queue_depths": {"plan": 500},
        }
        events = evaluate_alerts(snapshot)
        names = {e.rule_name for e in events}
        assert names == {"circuit_breaker_open", "queue_depth_exceeds"}

    def test_default_rules_used_when_unspecified(self) -> None:
        assert evaluate_alerts({}) == evaluate_alerts({}, DEFAULT_RULES)

    def test_custom_rules_override_defaults(self) -> None:
        custom_rule = AlertRule(
            name="always_fires",
            predicate=lambda snap: True,
            message_template="custom rule fired",
            severity="info",
        )
        events = evaluate_alerts({}, rules=[custom_rule])
        assert len(events) == 1
        assert events[0] == AlertEvent(
            rule_name="always_fires",
            severity="info",
            message="custom rule fired",
            snapshot={},
        )

    def test_rules_evaluated_in_order(self) -> None:
        snapshot = {
            "circuit_breakers": {"x": {"state": "open"}},
            "subscriber_health": {"state": "error"},
        }
        events = evaluate_alerts(snapshot)
        expected = [rule.name for rule in DEFAULT_RULES if rule.predicate(snapshot)]
        assert [event.rule_name for event in events] == expected


@pytest.mark.parametrize("rule", DEFAULT_RULES, ids=lambda r: r.name)
def test_default_rule_severity_is_valid(rule: AlertRule) -> None:
    """Every default rule uses a recognised severity level."""
    assert rule.severity in {"info", "warning", "critical"}
