"""Thread-safe metrics primitives and Prometheus text exposition.

Provides :class:`Counter` and :class:`Gauge` metric primitives, a
:class:`MetricsRegistry` that owns named instances (singleton per name,
mirroring the pattern in :mod:`hephaestus.resilience.circuit_breaker`), and
:func:`render_prometheus_text` which serialises a registry's state into the
Prometheus text exposition format.

Stdlib-only: no third-party dependency (e.g. ``prometheus_client``) is
required, keeping the base ``hephaestus`` import surface unchanged.
"""

from __future__ import annotations

import threading
from typing import Any

LabelKey = tuple[tuple[str, str], ...]


def _label_key(labels: dict[str, str] | None) -> LabelKey:
    """Normalise a labels dict into a hashable, order-independent key."""
    if not labels:
        return ()
    return tuple(sorted(labels.items()))


def _format_labels(labels: LabelKey) -> str:
    """Render a label key as Prometheus ``{k="v",...}`` syntax (empty if none)."""
    if not labels:
        return ""
    pairs = ",".join(f'{key}="{value}"' for key, value in labels)
    return "{" + pairs + "}"


class Counter:
    """A thread-safe, monotonically increasing metric, optionally labeled."""

    def __init__(self, name: str, help_text: str = "") -> None:
        """Create a counter named *name* with optional *help_text*."""
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: dict[LabelKey, float] = {}

    def inc(self, amount: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        """Increment the counter (for the given label set) by *amount*.

        Args:
            amount: Non-negative amount to add. Defaults to ``1.0``.
            labels: Optional label key/value pairs identifying the series.

        Raises:
            ValueError: If *amount* is negative.

        """
        if amount < 0:
            raise ValueError("Counter.inc() amount must be non-negative")
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def value(self, *, labels: dict[str, str] | None = None) -> float:
        """Return the current value for the given label set (0.0 if unset)."""
        key = _label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def snapshot(self) -> dict[LabelKey, float]:
        """Return a copy of all label-set values."""
        with self._lock:
            return dict(self._values)


class Gauge:
    """A thread-safe metric that can be set, incremented, or decremented."""

    def __init__(self, name: str, help_text: str = "") -> None:
        """Create a gauge named *name* with optional *help_text*."""
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._values: dict[LabelKey, float] = {}

    def set(self, value: float, *, labels: dict[str, str] | None = None) -> None:
        """Set the gauge's value for the given label set."""
        key = _label_key(labels)
        with self._lock:
            self._values[key] = value

    def inc(self, amount: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        """Increment the gauge (for the given label set) by *amount*."""
        key = _label_key(labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + amount

    def dec(self, amount: float = 1.0, *, labels: dict[str, str] | None = None) -> None:
        """Decrement the gauge (for the given label set) by *amount*."""
        self.inc(-amount, labels=labels)

    def value(self, *, labels: dict[str, str] | None = None) -> float:
        """Return the current value for the given label set (0.0 if unset)."""
        key = _label_key(labels)
        with self._lock:
            return self._values.get(key, 0.0)

    def snapshot(self) -> dict[LabelKey, float]:
        """Return a copy of all label-set values."""
        with self._lock:
            return dict(self._values)


class MetricsRegistry:
    """Thread-safe registry of named counters/gauges (singleton per name).

    A single instance is typically shared across a process (or a component
    such as the pipeline coordinator) so that ``/metrics`` reflects every
    metric registered anywhere in that process.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._lock = threading.Lock()
        self._metrics: dict[str, Counter | Gauge] = {}

    def counter(self, name: str, help_text: str = "") -> Counter:
        """Get or create a named :class:`Counter`.

        Args:
            name: Metric name. Must not already be registered as a gauge.
            help_text: Human-readable description (used on first creation).

        Returns:
            The registered :class:`Counter` instance.

        Raises:
            TypeError: If *name* is already registered as a different metric type.

        """
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                metric = Counter(name, help_text)
                self._metrics[name] = metric
                return metric
            if not isinstance(existing, Counter):
                existing_type = type(existing).__name__
                raise TypeError(f"metric '{name}' is already registered as {existing_type}")
            return existing

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        """Get or create a named :class:`Gauge`.

        Args:
            name: Metric name. Must not already be registered as a counter.
            help_text: Human-readable description (used on first creation).

        Returns:
            The registered :class:`Gauge` instance.

        Raises:
            TypeError: If *name* is already registered as a different metric type.

        """
        with self._lock:
            existing = self._metrics.get(name)
            if existing is None:
                metric = Gauge(name, help_text)
                self._metrics[name] = metric
                return metric
            if not isinstance(existing, Gauge):
                existing_type = type(existing).__name__
                raise TypeError(f"metric '{name}' is already registered as {existing_type}")
            return existing

    def snapshot(self) -> dict[str, float]:
        """Return a flat ``name`` -> value map, summing across label sets.

        Intended for lightweight in-process reads (e.g. alert evaluation)
        where per-label detail is not needed. Use :func:`render_prometheus_text`
        for a fully label-aware export.
        """
        with self._lock:
            metrics = list(self._metrics.values())
        return {metric.name: sum(metric.snapshot().values()) for metric in metrics}

    def all_metrics(self) -> dict[str, Counter | Gauge]:
        """Return a copy of the name -> metric mapping."""
        with self._lock:
            return dict(self._metrics)


def render_prometheus_text(registry: MetricsRegistry) -> str:
    """Render *registry*'s current state as Prometheus text exposition format.

    Args:
        registry: The registry to render.

    Returns:
        A string with one ``# HELP``/``# TYPE`` pair and one value line per
        label set, per metric, terminated with a trailing newline.

    """
    lines: list[str] = []
    for name, metric in sorted(registry.all_metrics().items()):
        metric_type = "counter" if isinstance(metric, Counter) else "gauge"
        if metric.help_text:
            lines.append(f"# HELP {name} {metric.help_text}")
        lines.append(f"# TYPE {name} {metric_type}")
        values = metric.snapshot()
        if not values:
            continue
        for label_key in sorted(values):
            lines.append(f"{name}{_format_labels(label_key)} {values[label_key]}")
    return "\n".join(lines) + "\n" if lines else ""


def _json_safe_snapshot(registry: MetricsRegistry) -> dict[str, Any]:
    """Return a JSON-serialisable view of *registry* (labels as nested dicts)."""
    result: dict[str, Any] = {}
    for name, metric in registry.all_metrics().items():
        series = [
            {"labels": dict(label_key), "value": value}
            for label_key, value in metric.snapshot().items()
        ]
        result[name] = series
    return result
