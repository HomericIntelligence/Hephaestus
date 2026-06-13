"""Shared pytest fixtures for the automation test package.

The circuit-breaker reset is autouse at PACKAGE scope (#708) so every test
under ``tests/unit/automation/`` starts with a clean breaker state. Without
this, a test that trips the GitHub API breaker (e.g. via rate-limit
exhaustion paths) leaves it OPEN, and any later test that exercises the
github_api / pr_reviewer code paths inherits the open breaker and fails with
``GitHub API circuit breaker is open due to sustained unavailability``
instead of the domain-specific error it was actually asserting against.

The fixture was previously declared only inside ``test_github_api.py`` —
which protected that file but not the rest of the package. Promoting it
here keeps the contract obvious (one autouse fixture, one place) without
expanding scope to tests that don't depend on automation primitives.
"""

from __future__ import annotations

import pytest

from hephaestus.resilience.circuit_breaker import reset_all_circuit_breakers


@pytest.fixture(autouse=True)
def _reset_circuit_breakers_for_automation_tests() -> None:
    """Reset all circuit breakers before each automation test.

    Prevents cross-test contamination when a prior test trips a breaker via
    rate-limit retries.
    """
    reset_all_circuit_breakers()


@pytest.fixture(autouse=True)
def _agents_authenticated_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the agent install+auth pre-flight (#1175) to pass by default.

    ``resolve_agent`` now refuses a ``--agent claude|codex`` selection unless the
    CLI is installed AND reports authenticated (#1175) — a real guard so an
    unauthenticated backend cannot silently produce empty output. But neither CLI
    is installed in CI, so every test that dispatches a named agent (mocking
    ``run_claude_text``/``run_codex_session``) would otherwise hit
    ``RuntimeError: Agent '...' is not installed``. Default the pre-flight to
    "authenticated"; tests exercising the unauthenticated/not-installed path
    override this with their own monkeypatch.
    """
    monkeypatch.setattr(
        "hephaestus.agents.runtime.is_agent_authenticated",
        lambda _agent: True,
    )
