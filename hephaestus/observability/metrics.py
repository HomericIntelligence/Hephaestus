"""Small, thread-safe Prometheus text exposition primitives.

The registry intentionally supports only counters and gauges, which cover the
live lifecycle values emitted by Hephaestus.  It does not open sockets, start
threads, or import product-layer code.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Mapping
from typing import TypeAlias, overload

_METRIC_NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\Z")
_LABEL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
LabelValues: TypeAlias = tuple[tuple[str, str], ...]


def _normalise_labels(labels: Mapping[str, object] | None) -> LabelValues:
    """Validate and canonicalise a Prometheus label mapping."""
    if labels is None:
        return ()
    result: list[tuple[str, str]] = []
    for name, value in labels.items():
        if not _LABEL_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid Prometheus label name: {name!r}")
        result.append((name, str(value)))
    return tuple(sorted(result))


def _validate_metric_name(name: str) -> None:
    """Reject invalid Prometheus metric names before they reach output."""
    if not _METRIC_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid Prometheus metric name: {name!r}")


def _escape_label_value(value: str) -> str:
    """Escape label values according to the Prometheus text format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _escape_help(value: str) -> str:
    """Escape HELP text according to the Prometheus text format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def _format_value(value: float) -> str:
    """Format a finite metric value without a needless decimal suffix."""
    if not math.isfinite(value):
        return "+Inf" if value > 0 else "-Inf" if value < 0 else "NaN"
    return format(value, "g")


class _Metric:
    """Shared storage and rendering mechanics for one metric family."""

    metric_type: str

    def __init__(self, name: str, help_text: str) -> None:
        _validate_metric_name(name)
        self.name = name
        self.help_text = help_text
        self._lock = threading.Lock()
        self._samples: dict[LabelValues, float] = {(): 0.0}
        # The first operation fixes the label schema.  Until then the initial
        # unlabeled zero is only a convenient default for a no-label metric.
        self._label_names: tuple[str, ...] | None = None

    def _sample_key(self, labels: Mapping[str, object] | None) -> LabelValues:
        key = _normalise_labels(labels)
        label_names = tuple(name for name, _ in key)
        with self._lock:
            if self._label_names is None:
                self._label_names = label_names
            elif self._label_names != label_names:
                raise ValueError(
                    f"metric {self.name!r} was registered with labels "
                    f"{self._label_names!r}, not {label_names!r}"
                )
            if self._label_names and () in self._samples:
                del self._samples[()]
            return key

    def _render_samples(self) -> tuple[str, str, list[tuple[LabelValues, float]]]:
        with self._lock:
            return self.name, self.help_text, sorted(self._samples.items())


class Counter(_Metric):
    """A monotonically increasing Prometheus counter."""

    metric_type = "counter"

    def inc(self, amount: float = 1.0, *, labels: Mapping[str, object] | None = None) -> None:
        """Increase the counter by a non-negative finite amount."""
        numeric_amount = float(amount)
        if not math.isfinite(numeric_amount) or numeric_amount < 0:
            raise ValueError("counter increments must be finite and non-negative")
        key = self._sample_key(labels)
        with self._lock:
            self._samples[key] = self._samples.get(key, 0.0) + numeric_amount


class Gauge(_Metric):
    """A Prometheus gauge that records an instantaneous finite value."""

    metric_type = "gauge"

    def set(self, value: float, *, labels: Mapping[str, object] | None = None) -> None:
        """Set the gauge to a finite numeric value."""
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise ValueError("gauge values must be finite")
        key = self._sample_key(labels)
        with self._lock:
            self._samples[key] = numeric_value


class MetricsRegistry:
    """Thread-safe named collection of counters and gauges."""

    def __init__(self) -> None:
        """Create an empty registry without any I/O or global state."""
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def counter(self, name: str, help_text: str = "") -> Counter:
        """Return the named counter, creating it when necessary."""
        return self._get_or_create(name, help_text, Counter)

    def gauge(self, name: str, help_text: str = "") -> Gauge:
        """Return the named gauge, creating it when necessary."""
        return self._get_or_create(name, help_text, Gauge)

    @overload
    def _get_or_create(self, name: str, help_text: str, cls: type[Counter]) -> Counter: ...

    @overload
    def _get_or_create(self, name: str, help_text: str, cls: type[Gauge]) -> Gauge: ...

    def _get_or_create(
        self, name: str, help_text: str, cls: type[Counter] | type[Gauge]
    ) -> Counter | Gauge:
        _validate_metric_name(name)
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = cls(name, help_text)
                self._metrics[name] = metric
            elif not isinstance(metric, cls):
                raise ValueError(f"metric {name!r} is already a {metric.metric_type}")
            elif help_text and metric.help_text != help_text:
                raise ValueError(f"metric {name!r} is already registered with different HELP text")
            return metric

    def render_prometheus(self) -> str:
        """Render this registry in Prometheus's text exposition format."""
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            name, help_text, samples = metric._render_samples()
            if help_text:
                lines.append(f"# HELP {name} {_escape_help(help_text)}")
            lines.append(f"# TYPE {name} {metric.metric_type}")
            for labels, value in samples:
                rendered_labels = ""
                if labels:
                    rendered_pairs = ",".join(
                        f'{label_name}="{_escape_label_value(label_value)}"'
                        for label_name, label_value in labels
                    )
                    rendered_labels = "{" + rendered_pairs + "}"
                lines.append(f"{name}{rendered_labels} {_format_value(value)}")
        return "\n".join(lines) + ("\n" if lines else "")


def render_prometheus_text(registry: MetricsRegistry) -> str:
    """Render *registry* as Prometheus text (a convenient functional API)."""
    return registry.render_prometheus()
