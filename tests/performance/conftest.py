"""Configuration for bounded worker-pool performance tests."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

_MAX_DURATION_S = 60.0
_MAX_JOBS = 100_000
_MAX_WORKERS = 32
_MAX_IN_FLIGHT = 256
_MAX_SERVICE_MS = 1_000.0
_MAX_P95_BUDGET_MS = 60_000.0


@dataclass(frozen=True)
class LoadConfig:
    """Validated profile for a bounded worker-pool load run."""

    duration_s: float
    max_jobs: int
    workers: int
    max_in_flight: int
    service_ms: float
    p95_budget_ms: float
    report_path: str

    @property
    def service_delay_s(self) -> float:
        """Return the synthetic service delay in seconds."""
        return self.service_ms / 1_000


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the explicit, bounded performance-lane options."""
    group = parser.getgroup("worker-pool performance")
    group.addoption("--load-duration-s", type=float, default=30.0)
    group.addoption("--load-max-jobs", type=int, default=50_000)
    group.addoption("--load-workers", type=int, default=8)
    group.addoption("--load-max-in-flight", type=int, default=64)
    group.addoption("--load-service-ms", type=float, default=5.0)
    group.addoption("--load-p95-budget-ms", type=float, default=500.0)
    group.addoption(
        "--load-report",
        type=str,
        default="build/performance/worker-pool.json",
    )


def _positive_bounded(
    config: pytest.Config,
    option: str,
    *,
    maximum: float | int,
) -> float | int:
    """Return a positive option value or stop before the load run begins."""
    value: float | int = config.getoption(option)
    if value <= 0:
        raise pytest.UsageError(f"{option} must be greater than zero")
    if value > maximum:
        raise pytest.UsageError(f"{option} must not exceed {maximum}")
    return value


@pytest.fixture(scope="session")
def load_config(pytestconfig: pytest.Config) -> LoadConfig:
    """Provide the immutable, validated profile for each performance test."""
    duration_s = _positive_bounded(
        pytestconfig,
        "load_duration_s",
        maximum=_MAX_DURATION_S,
    )
    max_jobs = _positive_bounded(pytestconfig, "load_max_jobs", maximum=_MAX_JOBS)
    workers = _positive_bounded(pytestconfig, "load_workers", maximum=_MAX_WORKERS)
    max_in_flight = _positive_bounded(
        pytestconfig,
        "load_max_in_flight",
        maximum=_MAX_IN_FLIGHT,
    )
    if max_in_flight < workers:
        raise pytest.UsageError("--load-max-in-flight must be at least --load-workers")
    service_ms = _positive_bounded(
        pytestconfig,
        "load_service_ms",
        maximum=_MAX_SERVICE_MS,
    )
    p95_budget_ms = _positive_bounded(
        pytestconfig,
        "load_p95_budget_ms",
        maximum=_MAX_P95_BUDGET_MS,
    )
    report_path = pytestconfig.getoption("load_report")
    if not report_path:
        raise pytest.UsageError("--load-report must not be empty")

    return LoadConfig(
        duration_s=float(duration_s),
        max_jobs=int(max_jobs),
        workers=int(workers),
        max_in_flight=int(max_in_flight),
        service_ms=float(service_ms),
        p95_budget_ms=float(p95_budget_ms),
        report_path=str(report_path),
    )
