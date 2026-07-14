"""Unit tests for hephaestus.observability.metrics."""

from __future__ import annotations

import threading

import pytest

from hephaestus.observability.metrics import (
    Counter,
    Gauge,
    MetricsRegistry,
    render_prometheus_text,
)


class TestCounter:
    """Tests for the Counter primitive."""

    def test_starts_at_zero(self) -> None:
        counter = Counter("requests_total")
        assert counter.value() == 0.0

    def test_inc_default_amount(self) -> None:
        counter = Counter("requests_total")
        counter.inc()
        assert counter.value() == 1.0

    def test_inc_custom_amount(self) -> None:
        counter = Counter("requests_total")
        counter.inc(5.0)
        counter.inc(2.5)
        assert counter.value() == 7.5

    def test_negative_amount_rejected(self) -> None:
        counter = Counter("requests_total")
        with pytest.raises(ValueError, match="non-negative"):
            counter.inc(-1.0)

    def test_labels_are_independent_series(self) -> None:
        counter = Counter("requests_total")
        counter.inc(labels={"stage": "plan"})
        counter.inc(3.0, labels={"stage": "implement"})
        assert counter.value(labels={"stage": "plan"}) == 1.0
        assert counter.value(labels={"stage": "implement"}) == 3.0
        assert counter.value(labels={"stage": "review"}) == 0.0

    def test_label_order_does_not_matter(self) -> None:
        counter = Counter("requests_total")
        counter.inc(labels={"a": "1", "b": "2"})
        counter.inc(labels={"b": "2", "a": "1"})
        assert counter.value(labels={"a": "1", "b": "2"}) == 2.0

    def test_concurrent_increments_are_thread_safe(self) -> None:
        counter = Counter("requests_total")
        n_threads = 20
        n_incs = 200

        def worker() -> None:
            for _ in range(n_incs):
                counter.inc()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert counter.value() == n_threads * n_incs

    def test_snapshot_returns_copy(self) -> None:
        counter = Counter("requests_total")
        counter.inc(labels={"stage": "plan"})
        snap = counter.snapshot()
        snap[(("stage", "mutated"),)] = 999.0
        assert counter.value(labels={"stage": "mutated"}) == 0.0


class TestGauge:
    """Tests for the Gauge primitive."""

    def test_starts_at_zero(self) -> None:
        gauge = Gauge("queue_depth")
        assert gauge.value() == 0.0

    def test_set_overwrites(self) -> None:
        gauge = Gauge("queue_depth")
        gauge.set(5.0)
        gauge.set(2.0)
        assert gauge.value() == 2.0

    def test_inc_and_dec(self) -> None:
        gauge = Gauge("queue_depth")
        gauge.inc(3.0)
        gauge.dec(1.0)
        assert gauge.value() == 2.0

    def test_dec_can_go_negative(self) -> None:
        gauge = Gauge("delta")
        gauge.dec(5.0)
        assert gauge.value() == -5.0

    def test_labels_are_independent_series(self) -> None:
        gauge = Gauge("queue_depth")
        gauge.set(3.0, labels={"stage": "plan"})
        gauge.set(7.0, labels={"stage": "implement"})
        assert gauge.value(labels={"stage": "plan"}) == 3.0
        assert gauge.value(labels={"stage": "implement"}) == 7.0

    def test_concurrent_inc_dec_is_thread_safe(self) -> None:
        gauge = Gauge("counter_like")
        n_threads = 20
        n_ops = 200

        def worker() -> None:
            for _ in range(n_ops):
                gauge.inc()

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert gauge.value() == n_threads * n_ops


class TestMetricsRegistry:
    """Tests for the MetricsRegistry singleton-per-name behavior."""

    def test_counter_is_singleton_per_name(self) -> None:
        registry = MetricsRegistry()
        c1 = registry.counter("requests_total")
        c2 = registry.counter("requests_total")
        assert c1 is c2

    def test_gauge_is_singleton_per_name(self) -> None:
        registry = MetricsRegistry()
        g1 = registry.gauge("queue_depth")
        g2 = registry.gauge("queue_depth")
        assert g1 is g2

    def test_help_text_set_on_first_creation(self) -> None:
        registry = MetricsRegistry()
        c1 = registry.counter("requests_total", "total requests")
        c2 = registry.counter("requests_total", "ignored on second call")
        assert c1.help_text == "total requests"
        assert c2.help_text == "total requests"

    def test_type_collision_raises(self) -> None:
        registry = MetricsRegistry()
        registry.counter("thing")
        with pytest.raises(TypeError, match="already registered"):
            registry.gauge("thing")

    def test_snapshot_sums_across_labels(self) -> None:
        registry = MetricsRegistry()
        counter = registry.counter("requests_total")
        counter.inc(labels={"stage": "plan"})
        counter.inc(2.0, labels={"stage": "implement"})
        assert registry.snapshot() == {"requests_total": 3.0}

    def test_snapshot_includes_all_metrics(self) -> None:
        registry = MetricsRegistry()
        registry.counter("a").inc(1.0)
        registry.gauge("b").set(2.0)
        assert registry.snapshot() == {"a": 1.0, "b": 2.0}

    def test_all_metrics_returns_copy(self) -> None:
        registry = MetricsRegistry()
        registry.counter("a")
        metrics = registry.all_metrics()
        metrics["injected"] = Counter("injected")
        assert "injected" not in registry.all_metrics()


class TestRenderPrometheusText:
    """Tests for the Prometheus text exposition renderer."""

    def test_empty_registry_produces_empty_string(self) -> None:
        registry = MetricsRegistry()
        assert render_prometheus_text(registry) == ""

    def test_counter_help_and_type_lines(self) -> None:
        registry = MetricsRegistry()
        registry.counter("requests_total", "total requests").inc(3.0)
        text = render_prometheus_text(registry)
        assert "# HELP requests_total total requests" in text
        assert "# TYPE requests_total counter" in text
        assert "requests_total 3.0" in text

    def test_gauge_type_line(self) -> None:
        registry = MetricsRegistry()
        registry.gauge("queue_depth").set(5.0)
        text = render_prometheus_text(registry)
        assert "# TYPE queue_depth gauge" in text
        assert "queue_depth 5.0" in text

    def test_labels_rendered_in_prometheus_syntax(self) -> None:
        registry = MetricsRegistry()
        registry.gauge("stage_queue_depth").set(4.0, labels={"stage": "plan"})
        text = render_prometheus_text(registry)
        assert 'stage_queue_depth{stage="plan"} 4.0' in text

    def test_no_help_text_omits_help_line(self) -> None:
        registry = MetricsRegistry()
        registry.counter("no_help").inc()
        text = render_prometheus_text(registry)
        assert "# HELP" not in text
        assert "# TYPE no_help counter" in text

    def test_metric_without_values_still_emits_type_line(self) -> None:
        registry = MetricsRegistry()
        registry.gauge("untouched")
        text = render_prometheus_text(registry)
        assert "# TYPE untouched gauge" in text

    def test_output_ends_with_newline(self) -> None:
        registry = MetricsRegistry()
        registry.counter("a").inc()
        text = render_prometheus_text(registry)
        assert text.endswith("\n")

    def test_metrics_sorted_by_name(self) -> None:
        registry = MetricsRegistry()
        registry.counter("zeta").inc()
        registry.counter("alpha").inc()
        text = render_prometheus_text(registry)
        assert text.index("alpha") < text.index("zeta")
