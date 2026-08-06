"""Behavioral tests for bounded Prometheus metric cardinality."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hephaestus.observability.metrics import MetricsRegistry


def test_allowed_label_dimensions_and_values_are_enforced() -> None:
    """Declared dimensions and finite values reject untrusted label input."""
    registry = MetricsRegistry()
    gauge = registry.gauge(
        "test_stage_depth",
        allowed_labels={"stage": {"repo", "planning"}},
        series_cap=2,
    )
    gauge.set(1, labels={"stage": "repo"})

    with pytest.raises(ValueError, match="registered with labels"):
        gauge.set(1, labels={"outcome": "ok"})
    with pytest.raises(ValueError, match="not allowed"):
        gauge.set(1, labels={"stage": "unknown"})


def test_label_values_are_bounded_by_escaped_utf8_size() -> None:
    """Label values at the byte boundary work, while oversized values are rejected."""
    registry = MetricsRegistry()
    counter = registry.counter(
        "test_label_size_total",
        allowed_labels={"tenant": None},
        series_cap=1,
    )
    boundary_value = "é" * 512

    counter.inc(labels={"tenant": boundary_value})
    with pytest.raises(ValueError, match="at most 1024 escaped UTF-8 bytes"):
        counter.inc(labels={"tenant": "\\" * 513})

    rendered = registry.render_prometheus()
    assert f'test_label_size_total{{tenant="{boundary_value}"}} 1' in rendered
    assert "hephaestus_metrics_series_overflow_total" not in rendered


def test_oversized_allowed_label_value_is_rejected_at_registration() -> None:
    """Finite label domains cannot retain values too large for exposition."""
    registry = MetricsRegistry()

    with pytest.raises(ValueError, match="at most 1024 escaped UTF-8 bytes"):
        registry.gauge(
            "test_allowed_label_size",
            allowed_labels={"tenant": {"x" * 1025}},
        )


def test_series_cap_is_per_family_and_overflow_is_exported() -> None:
    """Each family admits its own cap and exposes rejected new-series writes."""
    registry = MetricsRegistry(default_series_cap=2)
    one = registry.counter(
        "test_one_total",
        allowed_labels={"tenant": None},
        series_cap=1,
    )
    two = registry.counter("test_two_total", allowed_labels={"tenant": None})

    for tenant in ("a", "b"):
        one.inc(labels={"tenant": tenant})
    for tenant in ("a", "b", "c"):
        two.inc(labels={"tenant": tenant})

    rendered = registry.render_prometheus()
    assert rendered.count("test_one_total{") == 1
    assert rendered.count("test_two_total{") == 2
    assert 'hephaestus_metrics_series_overflow_total{family="test_one_total"} 1' in rendered
    assert 'hephaestus_metrics_series_overflow_total{family="test_two_total"} 1' in rendered


def test_admitted_series_remains_writable_after_overflow() -> None:
    """Dropping a new tuple never prevents updates to an admitted tuple."""
    registry = MetricsRegistry()
    counter = registry.counter(
        "test_stable_total",
        allowed_labels={"tenant": None},
        series_cap=1,
    )

    counter.inc(labels={"tenant": "admitted"})
    counter.inc(labels={"tenant": "discarded"})
    counter.inc(labels={"tenant": "admitted"})

    rendered = registry.render_prometheus()
    assert 'test_stable_total{tenant="admitted"} 2' in rendered
    assert 'test_stable_total{tenant="discarded"}' not in rendered
    assert 'hephaestus_metrics_series_overflow_total{family="test_stable_total"} 1' in rendered


def test_repeat_registration_rejects_conflicts_and_preserves_omitted_policy() -> None:
    """Omitted registration options do not weaken an existing family policy."""
    registry = MetricsRegistry(default_series_cap=3)
    counter = registry.counter(
        "test_registration_total",
        allowed_labels={"tenant": None},
        series_cap=2,
    )

    assert registry.counter("test_registration_total") is counter
    assert (
        registry.counter(
            "test_registration_total",
            allowed_labels={"tenant": None},
            series_cap=2,
        )
        is counter
    )
    with pytest.raises(ValueError, match="different labels"):
        registry.counter("test_registration_total", allowed_labels={"repo": None})
    with pytest.raises(ValueError, match="different series cap"):
        registry.counter("test_registration_total", series_cap=3)


def test_invalid_policies_and_reserved_overflow_family_are_rejected() -> None:
    """Invalid caps, domains, and the internal overflow family fail at registration."""
    registry = MetricsRegistry()

    with pytest.raises(ValueError, match="positive integer"):
        MetricsRegistry(default_series_cap=0)
    with pytest.raises(ValueError, match="positive integer"):
        registry.counter("test_invalid_total", series_cap=0)
    with pytest.raises(ValueError, match="label name"):
        registry.gauge("test_invalid_labels", allowed_labels={"not valid": None})
    with pytest.raises(ValueError, match="finite collection"):
        registry.gauge("test_invalid_domain", allowed_labels={"stage": "repo"})
    with pytest.raises(ValueError, match="reserved"):
        registry.counter("hephaestus_metrics_series_overflow_total")


def test_adversarial_churn_keeps_samples_and_rendering_bounded() -> None:
    """High-cardinality churn retains only admitted tuples and bounded text."""
    registry = MetricsRegistry()
    counter = registry.counter(
        "test_churn_total",
        allowed_labels={"tenant": None},
        series_cap=8,
    )

    for tenant in range(10_000):
        counter.inc(labels={"tenant": tenant})

    rendered = registry.render_prometheus()
    assert rendered.count("test_churn_total{") == 8
    assert 'hephaestus_metrics_series_overflow_total{family="test_churn_total"} 9992' in rendered
    assert len(rendered.encode("utf-8")) < 2_048


def test_concurrent_new_series_never_exceed_cap() -> None:
    """Concurrent admissions remain atomic and the overflow count is exact."""
    registry = MetricsRegistry()
    counter = registry.counter(
        "test_concurrent_total",
        allowed_labels={"tenant": None},
        series_cap=4,
    )
    barrier = threading.Barrier(16)

    def record(tenant: int) -> None:
        barrier.wait()
        counter.inc(labels={"tenant": tenant})

    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(record, range(16)))

    rendered = registry.render_prometheus()
    assert rendered.count("test_concurrent_total{") == 4
    assert 'hephaestus_metrics_series_overflow_total{family="test_concurrent_total"} 12' in rendered
