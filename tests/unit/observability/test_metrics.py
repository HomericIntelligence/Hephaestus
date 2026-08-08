"""Tests for Prometheus text metrics primitives."""

from __future__ import annotations

import importlib
import subprocess
import sys

import pytest


def test_registry_renders_counter_with_escaped_labels() -> None:
    """Counters render valid Prometheus labels without corrupting values."""
    metrics = importlib.import_module("hephaestus.observability.metrics")
    registry = metrics.MetricsRegistry()

    registry.counter("hephaestus_events_total", "Events processed").inc(
        labels={"source": 'a\\b\n"quoted"'}
    )

    rendered = registry.render_prometheus()

    assert "# HELP hephaestus_events_total Events processed" in rendered
    assert "# TYPE hephaestus_events_total counter" in rendered
    assert 'hephaestus_events_total{source="a\\\\b\\n\\"quoted\\""} 1' in rendered


def test_observability_import_opens_no_socket() -> None:
    """Importing the library package never starts a server or opens a socket."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import socket, ssl; OriginalSocket = socket.socket; "
            "socket.socket = type('NoSocket', (OriginalSocket,), {'__init__': "
            "lambda self, *a, **k: (_ for _ in ()).throw(AssertionError("
            "'socket opened during import'))}); import hephaestus.observability",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_registry_rejects_invalid_metric_and_label_names() -> None:
    """Untrusted names cannot produce malformed Prometheus exposition text."""
    metrics = importlib.import_module("hephaestus.observability.metrics")
    registry = metrics.MetricsRegistry()

    with pytest.raises(ValueError, match="metric name"):
        registry.counter("not valid")
    with pytest.raises(ValueError, match="label name"):
        registry.gauge("hephaestus_valid").set(1, labels={"not valid": "value"})
