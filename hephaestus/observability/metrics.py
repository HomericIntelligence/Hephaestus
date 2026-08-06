"""Small, thread-safe Prometheus text exposition primitives.

The registry intentionally supports only counters and gauges, which cover the
live lifecycle values emitted by Hephaestus.  It does not open sockets, start
threads, or import product-layer code.
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Collection, Mapping
from typing import overload

_METRIC_NAME_RE = re.compile(r"[A-Za-z_:][A-Za-z0-9_:]*\Z")
_LABEL_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_DEFAULT_SERIES_CAP = 100
_SERIES_OVERFLOW_METRIC = "hephaestus_metrics_series_overflow_total"
type LabelValues = tuple[tuple[str, str], ...]
type AllowedLabels = tuple[tuple[str, frozenset[str] | None], ...]


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


def _validate_series_cap(series_cap: int) -> int:
    """Validate a positive per-family series cap."""
    if isinstance(series_cap, bool) or not isinstance(series_cap, int) or series_cap <= 0:
        raise ValueError("series_cap must be a positive integer")
    return series_cap


def _normalise_allowed_labels(
    allowed_labels: Mapping[str, Collection[object] | None] | None,
) -> AllowedLabels | None:
    """Canonicalise a declared label schema and its optional finite domains."""
    if allowed_labels is None:
        return None
    if not isinstance(allowed_labels, Mapping):
        raise ValueError("allowed_labels must be a mapping")

    result: list[tuple[str, frozenset[str] | None]] = []
    for name, values in allowed_labels.items():
        if not isinstance(name, str) or not _LABEL_NAME_RE.fullmatch(name):
            raise ValueError(f"invalid Prometheus label name: {name!r}")
        if values is None:
            normalised_values = None
        else:
            if not isinstance(values, Collection) or isinstance(values, (str, bytes)):
                raise ValueError(f"allowed values for label {name!r} must be a finite collection")
            normalised_values = frozenset(str(value) for value in values)
        result.append((name, normalised_values))
    return tuple(sorted(result))


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

    def __init__(
        self,
        name: str,
        help_text: str,
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None = None,
        series_cap: int = _DEFAULT_SERIES_CAP,
    ) -> None:
        _validate_metric_name(name)
        self.name = name
        self.help_text = help_text
        self.series_cap = _validate_series_cap(series_cap)
        self._allowed_labels = _normalise_allowed_labels(allowed_labels)
        self._label_names = (
            tuple(name for name, _ in self._allowed_labels)
            if self._allowed_labels is not None
            else None
        )
        self._lock = threading.Lock()
        self._samples: dict[LabelValues, float] = {(): 0.0}
        self._overflow_total = 0

    def _validate_label_key_locked(self, key: LabelValues) -> None:
        """Validate a canonical label tuple while the family lock is held."""
        label_names = tuple(name for name, _ in key)
        if self._label_names is None:
            self._label_names = label_names
        elif self._label_names != label_names:
            raise ValueError(
                f"metric {self.name!r} was registered with labels "
                f"{self._label_names!r}, not {label_names!r}"
            )

        if self._allowed_labels is None:
            return
        allowed = dict(self._allowed_labels)
        for name, value in key:
            values = allowed[name]
            if values is not None and value not in values:
                raise ValueError(
                    f"metric {self.name!r} label {name!r} value {value!r} is not allowed"
                )

    def _write_sample(
        self,
        value: float,
        *,
        labels: Mapping[str, object] | None,
        additive: bool,
    ) -> None:
        """Admit and mutate one sample atomically under the family lock."""
        key = _normalise_labels(labels)
        with self._lock:
            self._validate_label_key_locked(key)
            if key and () in self._samples:
                del self._samples[()]
            if key not in self._samples and len(self._samples) >= self.series_cap:
                self._overflow_total += 1
                return
            current = self._samples.get(key, 0.0)
            self._samples[key] = current + value if additive else value

    def _render_samples(
        self,
    ) -> tuple[str, str, list[tuple[LabelValues, float]], int]:
        """Return a consistent snapshot of samples and rejected writes."""
        with self._lock:
            return (
                self.name,
                self.help_text,
                sorted(self._samples.items()),
                self._overflow_total,
            )


class Counter(_Metric):
    """A monotonically increasing Prometheus counter."""

    metric_type = "counter"

    def inc(self, amount: float = 1.0, *, labels: Mapping[str, object] | None = None) -> None:
        """Increase the counter by a non-negative finite amount."""
        numeric_amount = float(amount)
        if not math.isfinite(numeric_amount) or numeric_amount < 0:
            raise ValueError("counter increments must be finite and non-negative")
        self._write_sample(numeric_amount, labels=labels, additive=True)


class Gauge(_Metric):
    """A Prometheus gauge that records an instantaneous finite value."""

    metric_type = "gauge"

    def set(self, value: float, *, labels: Mapping[str, object] | None = None) -> None:
        """Set the gauge to a finite numeric value."""
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise ValueError("gauge values must be finite")
        self._write_sample(numeric_value, labels=labels, additive=False)


class MetricsRegistry:
    """Thread-safe named collection of counters and gauges."""

    def __init__(self, *, default_series_cap: int = _DEFAULT_SERIES_CAP) -> None:
        """Create an empty registry without any I/O or global state."""
        self._default_series_cap = _validate_series_cap(default_series_cap)
        self._lock = threading.Lock()
        self._metrics: dict[str, _Metric] = {}

    def counter(
        self,
        name: str,
        help_text: str = "",
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None = None,
        series_cap: int | None = None,
    ) -> Counter:
        """Return the named counter, creating it when necessary."""
        return self._get_or_create(
            name,
            help_text,
            Counter,
            allowed_labels=allowed_labels,
            series_cap=series_cap,
        )

    def gauge(
        self,
        name: str,
        help_text: str = "",
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None = None,
        series_cap: int | None = None,
    ) -> Gauge:
        """Return the named gauge, creating it when necessary."""
        return self._get_or_create(
            name,
            help_text,
            Gauge,
            allowed_labels=allowed_labels,
            series_cap=series_cap,
        )

    @overload
    def _get_or_create(
        self,
        name: str,
        help_text: str,
        cls: type[Counter],
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None,
        series_cap: int | None,
    ) -> Counter: ...

    @overload
    def _get_or_create(
        self,
        name: str,
        help_text: str,
        cls: type[Gauge],
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None,
        series_cap: int | None,
    ) -> Gauge: ...

    def _get_or_create(
        self,
        name: str,
        help_text: str,
        cls: type[Counter] | type[Gauge],
        *,
        allowed_labels: Mapping[str, Collection[object] | None] | None,
        series_cap: int | None,
    ) -> Counter | Gauge:
        _validate_metric_name(name)
        if name == _SERIES_OVERFLOW_METRIC:
            raise ValueError(f"metric name {name!r} is reserved")
        normalised_allowed_labels = _normalise_allowed_labels(allowed_labels)
        validated_series_cap = (
            self._default_series_cap if series_cap is None else _validate_series_cap(series_cap)
        )
        with self._lock:
            metric = self._metrics.get(name)
            if metric is None:
                metric = cls(
                    name,
                    help_text,
                    allowed_labels=allowed_labels,
                    series_cap=validated_series_cap,
                )
                self._metrics[name] = metric
            elif not isinstance(metric, cls):
                raise ValueError(f"metric {name!r} is already a {metric.metric_type}")
            elif help_text and metric.help_text != help_text:
                raise ValueError(f"metric {name!r} is already registered with different HELP text")
            elif allowed_labels is not None and metric._allowed_labels != normalised_allowed_labels:
                raise ValueError(f"metric {name!r} is already registered with different labels")
            elif series_cap is not None and metric.series_cap != validated_series_cap:
                raise ValueError(
                    f"metric {name!r} is already registered with a different series cap"
                )
            return metric

    def render_prometheus(self) -> str:
        """Render this registry in Prometheus's text exposition format."""
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        overflows: list[tuple[str, int]] = []
        for metric in metrics:
            name, help_text, samples, overflow_total = metric._render_samples()
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
            if overflow_total:
                overflows.append((name, overflow_total))
        if overflows:
            lines.append(
                "# HELP hephaestus_metrics_series_overflow_total "
                "New metric-series updates discarded after reaching a family cap."
            )
            lines.append("# TYPE hephaestus_metrics_series_overflow_total counter")
            for family, total in sorted(overflows):
                lines.append(
                    "hephaestus_metrics_series_overflow_total"
                    f'{{family="{_escape_label_value(family)}"}} {_format_value(total)}'
                )
        return "\n".join(lines) + ("\n" if lines else "")


def render_prometheus_text(registry: MetricsRegistry) -> str:
    """Render *registry* as Prometheus text (a convenient functional API)."""
    return registry.render_prometheus()
