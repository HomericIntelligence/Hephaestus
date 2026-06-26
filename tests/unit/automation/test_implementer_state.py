"""Tests for implementation state persistence helpers."""

from pathlib import Path

import pytest

from hephaestus.automation.implementer_state import ImplementationStateManager
from hephaestus.automation.models import ImplementationState


def test_save_imports_canonical_write_secure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Saving state should resolve the canonical IO write_secure helper."""
    calls: list[tuple[Path, str]] = []

    def fake_write_secure(
        path: str | Path,
        content: str,
        permissions: int = 0o600,
    ) -> None:
        del permissions
        calls.append((Path(path), content))

    monkeypatch.setattr("hephaestus.io.utils.write_secure", fake_write_secure)

    manager = ImplementationStateManager(tmp_path)
    state = ImplementationState(issue_number=1401)
    manager.save(state)

    assert calls == [
        (tmp_path / "issue-1401.json", state.model_dump_json(indent=2)),
    ]
