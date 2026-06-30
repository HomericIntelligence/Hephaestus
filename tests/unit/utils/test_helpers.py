"""Tests for hephaestus.utils.helpers module-level configuration."""

from __future__ import annotations

import importlib

import pytest

from hephaestus.utils import helpers


def test_subprocess_timeout_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """The module-level timeouts honour valid integer overrides on reimport."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "33")
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "44")

    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.METADATA_TIMEOUT == 33
        assert reloaded.NETWORK_TIMEOUT == 44
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", raising=False)
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", raising=False)
        importlib.reload(helpers)


def test_subprocess_timeouts_fall_back_on_malformed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A malformed timeout override must not crash ``import hephaestus.utils.helpers``.

    Regression for #1429: bare ``int(os.environ.get(...))`` raised ``ValueError``
    at module-import time on non-numeric input; the safe ``read_timeout_env``
    helper logs a warning and falls back to the default instead.
    """
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "not-an-int")
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "12.5")

    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.METADATA_TIMEOUT == 10
        assert reloaded.NETWORK_TIMEOUT == 120
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", raising=False)
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", raising=False)
        importlib.reload(helpers)
