"""Tests for implementation-state persistence."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

import hephaestus.automation.implementer_state as implementer_state_module
from hephaestus.automation.models import ImplementationState
from hephaestus.io.utils import write_secure as io_write_secure


def test_implementation_state_manager_imports_canonical_write_secure() -> None:
    """The state manager imports the canonical secure writer directly."""
    assert vars(implementer_state_module)["write_secure"] is io_write_secure


def test_save_imports_canonical_write_secure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Saving state should resolve the canonical IO write_secure helper.

    Patches the module-level name in implementer_state (top-level import)
    rather than the source module, since the reference is bound at import time.
    """
    calls: list[tuple[Path, str]] = []

    def fake_write_secure(
        path: str | Path,
        content: str,
        permissions: int = 0o600,
    ) -> None:
        del permissions
        calls.append((Path(path), content))

    monkeypatch.setattr(implementer_state_module, "write_secure", fake_write_secure)

    manager = implementer_state_module.ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=1401)
    manager.save(state)

    assert calls == [
        (tmp_path / "issue-1401.json", state.model_dump_json(indent=2)),
    ]


def test_save_persists_issue_state_with_secure_permissions(tmp_path: Path) -> None:
    """save() writes issue-<n>.json atomically through write_secure."""
    manager = implementer_state_module.ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=1402, branch_name="issue-1402")

    manager.save(state)

    state_file = tmp_path / "issue-1402.json"
    restored = ImplementationState.model_validate_json(state_file.read_text())
    assert restored.issue_number == 1402
    assert restored.branch_name == "issue-1402"
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
