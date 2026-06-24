"""Regression matrix for persisted session provider metadata."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hephaestus.agents.runtime import session_agent_matches
from hephaestus.automation.address_review import AddressReviewer
from hephaestus.automation.ci_driver import CIDriver
from hephaestus.automation.models import AddressReviewOptions, CIDriverOptions


@pytest.mark.parametrize("selected_agent", ["claude", "codex", "pi"])
@pytest.mark.parametrize("saved_agent", [None, "claude", "codex", "pi"])
def test_session_agent_matches_matrix(
    saved_agent: str | None,
    selected_agent: str,
) -> None:
    """Missing metadata remains Claude-only; explicit metadata must match exactly."""
    expected = (saved_agent or "claude") == selected_agent
    assert session_agent_matches(saved_agent, selected_agent) is expected


@pytest.fixture
def address_reviewer_factory(tmp_path: Path) -> Callable[[str], AddressReviewer]:
    """Create address-review instances with isolated state dirs."""

    def _make(agent: str) -> AddressReviewer:
        reviewer = AddressReviewer(
            AddressReviewOptions(
                issues=[1578],
                agent=agent,
                max_workers=1,
                dry_run=False,
                enable_ui=False,
            ),
            get_repo_root=lambda: tmp_path,
            worktree_manager_factory=MagicMock(return_value=MagicMock()),
            status_tracker_factory=MagicMock(return_value=MagicMock()),
            log_manager_factory=MagicMock(return_value=MagicMock()),
        )
        reviewer.state_dir = tmp_path
        return reviewer

    return _make


@pytest.fixture
def ci_driver_factory(tmp_path: Path) -> Callable[[str], CIDriver]:
    """Create CI-driver instances with isolated state dirs."""

    def _make(agent: str) -> CIDriver:
        driver = CIDriver(
            CIDriverOptions(
                issues=[1578],
                agent=agent,
                max_workers=1,
                dry_run=False,
                enable_ui=False,
            )
        )
        driver.repo_root = tmp_path
        driver.state_dir = tmp_path
        return driver

    return _make


@pytest.mark.parametrize(
    ("selected_agent", "saved_agent", "expected"),
    [
        ("pi", "pi", "session-1578"),
        ("pi", "codex", None),
        ("pi", "claude", None),
        ("pi", None, None),
        ("codex", "pi", None),
        ("claude", "pi", None),
    ],
)
def test_address_review_load_impl_session_id_rejects_cross_provider_state(
    address_reviewer_factory: Callable[[str], AddressReviewer],
    tmp_path: Path,
    selected_agent: str,
    saved_agent: str | None,
    expected: str | None,
) -> None:
    """Address-review must never resume an implementer session from another provider."""
    payload = {"session_id": "session-1578"}
    if saved_agent is not None:
        payload["session_agent"] = saved_agent
    (tmp_path / "issue-1578.json").write_text(json.dumps(payload), encoding="utf-8")

    reviewer = address_reviewer_factory(selected_agent)

    assert reviewer._load_impl_session_id(1578) == expected


@pytest.mark.parametrize(
    ("selected_agent", "saved_agent", "expected"),
    [
        ("pi", "pi", "session-1578"),
        ("pi", "codex", None),
        ("pi", "claude", None),
        ("pi", None, None),
        ("codex", "pi", None),
        ("claude", "pi", None),
    ],
)
def test_ci_driver_load_impl_session_id_rejects_cross_provider_state(
    ci_driver_factory: Callable[[str], CIDriver],
    tmp_path: Path,
    selected_agent: str,
    saved_agent: str | None,
    expected: str | None,
) -> None:
    """CI repair must respect persisted provider metadata before resuming."""
    payload = {"session_id": "session-1578"}
    if saved_agent is not None:
        payload["session_agent"] = saved_agent
    (tmp_path / "issue-1578.json").write_text(json.dumps(payload), encoding="utf-8")

    driver = ci_driver_factory(selected_agent)

    assert driver._load_impl_session_id(1578) == expected
