"""Check installed runtime identity without network access."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from hephaestus.automation import runtime_diagnostics


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"vcs_info": {"commit_id": "a" * 40}}, "a" * 40),
        ({"dir_info": {"editable": True}}, None),
        ({"vcs_info": {"commit_id": "not-a-commit"}}, None),
        ({}, None),
    ],
)
def test_identity_uses_only_installed_vcs_commit(
    monkeypatch: pytest.MonkeyPatch, metadata: dict[str, object], expected: str | None
) -> None:
    """The target checkout cannot supply a missing installed commit."""
    package = MagicMock(version="1.2.3")
    package.read_text.return_value = json.dumps(metadata)
    monkeypatch.setattr(runtime_diagnostics, "distribution", lambda name: package)
    identity = runtime_diagnostics.runtime_identity()
    assert identity["installed_commit"] == expected
    assert identity["distribution_version"] == "1.2.3"
