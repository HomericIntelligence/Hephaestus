"""Tests for per-issue implementation state persistence."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from pydantic import ValidationError

from hephaestus.automation.models import ImplementationPhase, ImplementationState
from hephaestus.automation.state.implementer import ImplementationStateManager


def test_save_persists_issue_state_file(tmp_path: Path) -> None:
    """ImplementationStateManager.save writes the expected issue state file."""
    manager = ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=123, phase=ImplementationPhase.IMPLEMENTING)

    manager.save(state)

    restored = ImplementationState.model_validate_json((tmp_path / "issue-123.json").read_text())
    assert restored.issue_number == 123
    assert restored.phase is ImplementationPhase.IMPLEMENTING


def test_save_persists_issue_state_with_secure_permissions(tmp_path: Path) -> None:
    """save() writes issue-<n>.json atomically with 0o600 permissions.

    The state manager now routes writes through ``save_state_file`` (which
    delegates to the canonical ``write_secure``), so this asserts the
    observable secure-permission behavior rather than the import location.
    """
    manager = ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=1402, branch_name="issue-1402")

    manager.save(state)

    state_file = tmp_path / "issue-1402.json"
    restored = ImplementationState.model_validate_json(state_file.read_text())
    assert restored.issue_number == 1402
    assert restored.branch_name == "issue-1402"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600


def test_save_persists_session_provider_metadata(tmp_path: Path) -> None:
    """A valid session ID and provider survive the secure write boundary."""
    manager = ImplementationStateManager(tmp_path)
    state = ImplementationState(
        issue_number=1403,
        session_id="opaque",
        session_agent="codex",
    )

    manager.save(state)

    payload = json.loads((tmp_path / "issue-1403.json").read_text())
    assert payload["session_id"] == "opaque"
    assert payload["session_agent"] == "codex"


def test_save_rejects_mutated_session_without_provider(tmp_path: Path) -> None:
    """Save revalidates models mutated after construction."""
    manager = ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=123)
    state.session_id = "opaque"

    with pytest.raises(ValidationError):
        manager.save(state)

    assert not (tmp_path / "issue-123.json").exists()


def test_load_all_loads_valid_state_and_skips_corrupt_file(tmp_path: Path) -> None:
    """ImplementationStateManager.load_all keeps valid files and skips corrupt files."""
    valid = ImplementationState(issue_number=123, phase=ImplementationPhase.TESTING)
    (tmp_path / "issue-123.json").write_text(valid.model_dump_json())
    (tmp_path / "issue-456.json").write_text("{not valid json")

    manager = ImplementationStateManager(tmp_path)
    manager.load_all()

    assert manager.states[123].phase is ImplementationPhase.TESTING
    assert 456 not in manager.states


def test_load_all_skips_legacy_session_without_provider(tmp_path: Path) -> None:
    """Legacy session state is skipped instead of being inferred as Claude."""
    (tmp_path / "issue-123.json").write_text(
        json.dumps({"issue_number": 123, "session_id": "legacy"})
    )

    manager = ImplementationStateManager(tmp_path)
    manager.load_all()

    assert 123 not in manager.states


def test_load_only_hydrates_requested_issue_states(tmp_path: Path) -> None:
    """Issue-scoped runs should not load every stale issue state file."""
    current = ImplementationState(issue_number=123, phase=ImplementationPhase.TESTING)
    stale = ImplementationState(issue_number=456, phase=ImplementationPhase.FOLLOW_UP_ISSUES)
    (tmp_path / "issue-123.json").write_text(current.model_dump_json())
    (tmp_path / "issue-456.json").write_text(stale.model_dump_json())

    manager = ImplementationStateManager(tmp_path)
    manager.load_only([123])

    assert set(manager.states) == {123}
    assert manager.states[123].phase is ImplementationPhase.TESTING
