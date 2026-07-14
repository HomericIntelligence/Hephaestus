"""Observability primitives: metrics, an HTTP scrape/health server, and alerts.

Stdlib-only (no ``prometheus_client`` or other third-party dependency), so
this package is part of the base ``hephaestus`` import surface and may be
used by library code such as :mod:`hephaestus.nats.subscriber` and
:mod:`hephaestus.resilience.circuit_breaker`, as well as by the automation
product layer's pipeline coordinator.
"""

from __future__ import annotations

from hephaestus.observability.alerts import DEFAULT_RULES, AlertEvent, AlertRule, evaluate_alerts
from hephaestus.observability.metrics import (
    Counter,
    Gauge,
    MetricsRegistry,
    render_prometheus_text,
)
from hephaestus.observability.server import MetricsHTTPServer

__all__ = [
    "DEFAULT_RULES",
    "AlertEvent",
    "AlertRule",
    "Counter",
    "Gauge",
    "MetricsHTTPServer",
    "MetricsRegistry",
    "evaluate_alerts",
    "render_prometheus_text",
]
