"""Regression guard: p95 must include the slow tail (issue #2229)."""

from __future__ import annotations

from math import ceil

import pytest

from tests.performance.test_worker_pool_load import _latency_summary, _percentile


def _nearest_rank_p95(samples: list[float]) -> float:
    """Reproduce the pre-#2229 nearest-rank formula: ``ceil(n*p) - 1``, clamped."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, ceil(len(ordered) * 0.95) - 1))
    return ordered[index]


@pytest.mark.performance
def test_prior_nearest_rank_underreported_the_tail() -> None:
    """Document the defect: nearest-rank p95 excluded the slowest 5%."""
    samples = [10.0] * 95 + [9_999.0] * 5
    # Old behavior missed the entire slow tail...
    assert _nearest_rank_p95(samples) == 10.0
    # ...while the repaired interpolation reports a tail-inclusive value.
    assert _latency_summary(samples).p95 > 500.0


@pytest.mark.performance
def test_repaired_p95_catches_a_single_slow_completion() -> None:
    """Ten samples with one slow tenth yield an interpolated tail p95."""
    assert _percentile([10.0] * 9 + [1_000.0], 0.95) == pytest.approx(554.5)
