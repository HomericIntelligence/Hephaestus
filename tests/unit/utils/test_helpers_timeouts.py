"""Tests for the import-time subprocess-timeout env reads in ``helpers``.

``METADATA_TIMEOUT`` and ``NETWORK_TIMEOUT`` are bound at import time, so a
malformed override must fall back to the default rather than raising
``ValueError`` and crashing the module import. These tests set the env var,
reload the module, and assert the fallback / override behavior.
"""

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


def test_network_timeout_reads_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid integer NETWORK override is honored."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "45")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.NETWORK_TIMEOUT == 45
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", raising=False)
        importlib.reload(helpers)


def test_metadata_timeout_reads_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A valid integer METADATA override is honoured on reimport."""
    monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "33")
    reloaded = importlib.reload(helpers)
    try:
        assert reloaded.METADATA_TIMEOUT == 33
    finally:
        monkeypatch.delenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", raising=False)
        importlib.reload(helpers)
