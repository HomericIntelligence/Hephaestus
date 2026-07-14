"""Threshold-based alert evaluation over an observability snapshot.

Pure functions, no I/O: :func:`evaluate_alerts` takes a JSON-serialisable
snapshot dict (e.g. circuit breaker states plus stage queue depths) and
returns structured :class:`AlertEvent` objects for any :class:`AlertRule`
whose predicate matches. Callers are expected to log the returned events
(e.g. via the existing :class:`hephaestus.logging.formatters.JsonFormatter`)
so an external system (Prometheus/Alertmanager) can act on them; this
module does not push to any external alerting service.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_QUEUE_DEPTH_THRESHOLD: int = 100
"""Default stage-queue-depth alert threshold, used when a snapshot omits one."""


@dataclass(frozen=True)
class AlertRule:
    """A named threshold rule evaluated against a snapshot dict."""

    name: str
    predicate: Callable[[dict[str, Any]], bool]
    message_template: str
    severity: str = "warning"


@dataclass(frozen=True)
class AlertEvent:
    """A fired alert: the rule that matched, plus the snapshot that triggered it."""

    rule_name: str
    severity: str
    message: str
    snapshot: dict[str, Any]


def _circuit_breaker_open(snapshot: dict[str, Any]) -> bool:
    breakers = snapshot.get("circuit_breakers", {})
    return any(cb.get("state") == "open" for cb in breakers.values())


def _queue_depth_exceeds(snapshot: dict[str, Any]) -> bool:
    threshold = snapshot.get("queue_depth_threshold", DEFAULT_QUEUE_DEPTH_THRESHOLD)
    depths = snapshot.get("queue_depths", {})
    return any(depth > threshold for depth in depths.values())


def _subscriber_stalled(snapshot: dict[str, Any]) -> bool:
    health = snapshot.get("subscriber_health")
    if not health:
        return False
    return bool(health.get("state") == "error")


DEFAULT_RULES: tuple[AlertRule, ...] = (
    AlertRule(
        name="circuit_breaker_open",
        predicate=_circuit_breaker_open,
        message_template="one or more circuit breakers are OPEN",
        severity="critical",
    ),
    AlertRule(
        name="queue_depth_exceeds",
        predicate=_queue_depth_exceeds,
        message_template="stage queue depth exceeds threshold",
        severity="warning",
    ),
    AlertRule(
        name="subscriber_stalled",
        predicate=_subscriber_stalled,
        message_template="NATS subscriber is in ERROR state",
        severity="critical",
    ),
)
"""Alert rules covering the gaps called out in issue #1485: circuit breaker
state nobody reads, unbounded stage queue growth, and a stalled NATS
subscriber whose health_dict() is never surfaced."""


def evaluate_alerts(
    snapshot: dict[str, Any], rules: Sequence[AlertRule] = DEFAULT_RULES
) -> list[AlertEvent]:
    """Evaluate *rules* against *snapshot*, returning every rule that fires.

    Args:
        snapshot: A JSON-serialisable dict describing current system state
            (e.g. ``circuit_breakers``, ``queue_depths``, ``subscriber_health``).
        rules: Rules to evaluate. Defaults to :data:`DEFAULT_RULES`.

    Returns:
        One :class:`AlertEvent` per rule whose predicate returned ``True``,
        in rule order.

    """
    events: list[AlertEvent] = []
    for rule in rules:
        if rule.predicate(snapshot):
            events.append(
                AlertEvent(
                    rule_name=rule.name,
                    severity=rule.severity,
                    message=rule.message_template,
                    snapshot=snapshot,
                )
            )
    return events
