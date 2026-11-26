"""Fixtures for CLI entry-point unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_startup_update_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep entry-wiring tests off the package index.

    These tests drive console-script dispatch, not release-metadata fetching. A
    test that verifies the wiring itself overrides this stub with its own
    monkeypatch (last writer wins).
    """
    monkeypatch.setattr("hephaestus.cli.automation_entry.check_for_updates", lambda: False)
