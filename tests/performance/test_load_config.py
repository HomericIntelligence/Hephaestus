"""Unit coverage for bounded worker-pool load configuration."""

from __future__ import annotations

from typing import cast
from unittest.mock import MagicMock

import pytest

from tests.performance.conftest import _load_config_from_options


def _config_with_options(**overrides: float | int | str) -> pytest.Config:
    """Return a pytest-config double with the approved default profile."""
    options: dict[str, float | int | str] = {
        "load_duration_s": 30.0,
        "load_max_jobs": 50_000,
        "load_workers": 8,
        "load_max_in_flight": 64,
        "load_service_ms": 5.0,
        "load_p95_budget_ms": 500.0,
        "load_report": "build/performance/worker-pool.json",
    }
    options.update(overrides)
    config = MagicMock()
    config.getoption.side_effect = options.__getitem__
    return cast(pytest.Config, config)


@pytest.mark.performance
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("load_duration_s", 0.0),
        ("load_max_jobs", 100_001),
        ("load_workers", 33),
        ("load_max_in_flight", 257),
    ],
)
def test_load_config_rejects_non_positive_or_over_ceiling_values(
    option: str,
    value: float | int,
) -> None:
    """Safety bounds reject invalid profiles before the load handler starts."""
    with pytest.raises(pytest.UsageError):
        _load_config_from_options(_config_with_options(**{option: value}))


@pytest.mark.performance
def test_load_config_requires_enough_in_flight_work_for_every_worker() -> None:
    """Sustained profiles must let every configured worker become active."""
    with pytest.raises(pytest.UsageError, match="load_max_in_flight"):
        _load_config_from_options(
            _config_with_options(load_workers=8, load_max_in_flight=7)
        )
