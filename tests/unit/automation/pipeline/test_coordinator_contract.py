"""Prove the explicit coordinator host protocol with a real mypy run.

A source-text assertion cannot show that mypy rejects an unknown host member.
These tests therefore type-check small probe modules with the active
interpreter and inspect the diagnostics.

The probes live under ``build/``. The mypy cache is shared between the two
probes so the second check reuses the first one's work.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROBE_ROOT = _REPO_ROOT / "build" / "coordinator-contract-probes"
_CACHE_DIR = _PROBE_ROOT / ".mypy_cache"
# A cold check reads the whole coordinator import graph.
_TIMEOUT_S = 600

_VALID_PROBE = '''\
"""A coordinator must satisfy the host protocol."""

from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost


def _reads_declared_state(host: _CoordinatorHost) -> int:
    return host._stalled_ticks


def _coordinator_is_a_host(coordinator: Coordinator) -> int:
    return _reads_declared_state(coordinator)
'''

_INVALID_CALL_PROBE = '''\
"""An unknown host member accessed directly must be rejected."""

from hephaestus.automation.pipeline.coordinator import Coordinator


def _rejects_unknown_member(coordinator: Coordinator) -> None:
    coordinator.definitely_not_a_coordinator_member()
'''


def _write_probe(name: str, source: str) -> Path:
    """Write one probe module under ``build/`` and return its path."""
    _PROBE_ROOT.mkdir(parents=True, exist_ok=True)
    probe = _PROBE_ROOT / name
    probe.write_text(source, encoding="utf-8")
    return probe


def _run_mypy(probe: Path) -> subprocess.CompletedProcess[str]:
    """Type-check one probe with the active interpreter and a bounded timeout."""
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--cache-dir", str(_CACHE_DIR), str(probe)],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        check=False,
    )


@pytest.fixture(scope="module", autouse=True)
def _clean_probes() -> Iterator[None]:
    """Remove probe modules after the module finishes, keeping the cache."""
    yield
    if _PROBE_ROOT.exists():
        for entry in _PROBE_ROOT.iterdir():
            if entry.is_dir():
                continue
            entry.unlink()


def test_coordinator_satisfies_the_host_protocol() -> None:
    """A concrete coordinator is accepted wherever the protocol is required."""
    probe = _write_probe("probe_valid_host.py", _VALID_PROBE)

    completed = _run_mypy(probe)

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "error:" not in completed.stdout


def test_unknown_host_member_is_rejected() -> None:
    """An absent member produces its own attr-defined diagnostic."""
    probe = _write_probe("probe_invalid_host.py", _INVALID_CALL_PROBE)

    completed = _run_mypy(probe)

    output = completed.stdout + completed.stderr
    assert completed.returncode != 0, output
    # A dependency or import failure is not evidence of rejection.
    assert "definitely_not_a_coordinator_member" in output
    assert "attr-defined" in output
    errors = [line for line in completed.stdout.splitlines() if ": error:" in line]
    assert len(errors) == 1, output
    assert probe.name in errors[0]
