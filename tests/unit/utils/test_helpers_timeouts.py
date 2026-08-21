"""Poison-environment tests for fixed subprocess timeout defaults."""

import importlib

import pytest

import hephaestus.utils.helpers as helpers


def test_metadata_timeout_uses_default_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer METADATA override falls back to the default, not ValueError."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "not-an-int")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.METADATA_TIMEOUT == 10  # falls back, no ValueError at import
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", raising=False)
        importlib.reload(helpers)  # restore clean module state for other tests


def test_network_timeout_uses_default_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-integer NETWORK override falls back to the default, not ValueError."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "not-an-int")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.NETWORK_TIMEOUT == 120  # falls back, no ValueError at import
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", raising=False)
        importlib.reload(helpers)


def test_network_timeout_ignores_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retired NETWORK override has no effect."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "45")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.NETWORK_TIMEOUT == 120
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", raising=False)
        importlib.reload(helpers)


def test_metadata_timeout_ignores_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retired METADATA override has no effect."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "33")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.METADATA_TIMEOUT == 10
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", raising=False)
        importlib.reload(helpers)
