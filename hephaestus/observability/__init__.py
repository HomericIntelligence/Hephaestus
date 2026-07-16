"""Stdlib-only runtime observability primitives.

The package deliberately has no import-time I/O or optional dependencies.  A
caller may create a registry alone, or explicitly start the local HTTP server
when it wants a scrape endpoint.
"""

from __future__ import annotations

from hephaestus.observability.alerts import AlertEvent, AlertTracker, evaluate_alerts
from hephaestus.observability.metrics import Counter, Gauge, MetricsRegistry
from hephaestus.observability.server import MetricsHTTPServer

__all__ = [
    "AlertEvent",
    "AlertTracker",
    "Counter",
    "Gauge",
    "MetricsHTTPServer",
    "MetricsRegistry",
    "evaluate_alerts",
]
