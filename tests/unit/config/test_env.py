#!/usr/bin/env python3
"""Tests for the centralized environment variable registry.

Covers :mod:`hephaestus.config.env` — typed accessors, registration,
registered-default fallbacks, fail-open coercion, and diagnostics helpers.
"""

from __future__ import annotations

import logging

import pytest

from hephaestus.config.env import EnvRegistry, EnvVarSpec, env


@pytest.fixture
def reg() -> EnvRegistry:
    """Return a fresh, empty registry isolated from the module singleton."""
    return EnvRegistry()


# ── Spec ──────────────────────────────────────────────────────────────


def test_envvarspec_is_frozen() -> None:
    """An EnvVarSpec is immutable after construction."""
    spec = EnvVarSpec(name="HEPH_X", type=int, default=1, description="d")
    with pytest.raises((AttributeError, TypeError)):
        spec.name = "other"  # type: ignore[misc]


# ── Registration ──────────────────────────────────────────────────────


def test_register_and_spec(reg: EnvRegistry) -> None:
    """register() stores a spec that spec() returns verbatim."""
    reg.register("HEPH_X", type=int, default=7, description="desc")
    spec = reg.spec("HEPH_X")
    assert spec is not None
    assert spec.name == "HEPH_X"
    assert spec.type is int
    assert spec.default == 7
    assert spec.description == "desc"


def test_spec_missing_returns_none(reg: EnvRegistry) -> None:
    """spec() returns None for an unregistered name."""
    assert reg.spec("HEPH_NOPE") is None


def test_register_overwrites_silently(reg: EnvRegistry) -> None:
    """Re-registering a name replaces the previous spec without error."""
    reg.register("HEPH_X", type=int, default=1)
    reg.register("HEPH_X", type=str, default="z")
    spec = reg.spec("HEPH_X")
    assert spec is not None
    assert spec.type is str
    assert spec.default == "z"


def test_all_specs_returns_snapshot_copy(reg: EnvRegistry) -> None:
    """all_specs returns a copy; mutating it does not touch the registry."""
    reg.register("HEPH_X", type=int, default=1)
    specs = reg.all_specs
    specs.clear()
    assert reg.spec("HEPH_X") is not None


# ── str accessor ──────────────────────────────────────────────────────


def test_str_reads_env(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """str() returns the live environment value when set."""
    monkeypatch.setenv("HEPH_S", "hello")
    assert reg.str("HEPH_S") == "hello"


def test_str_unset_uses_explicit_default(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """str() falls back to the explicit default when the var is unset."""
    monkeypatch.delenv("HEPH_S", raising=False)
    assert reg.str("HEPH_S", default="fallback") == "fallback"


def test_str_unset_uses_registered_default(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """str() uses the registered default when unset and no explicit default."""
    monkeypatch.delenv("HEPH_S", raising=False)
    reg.register("HEPH_S", type=str, default="registered")
    assert reg.str("HEPH_S") == "registered"


def test_str_unset_unregistered_returns_empty(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """str() returns the empty string for an unset, unregistered var."""
    monkeypatch.delenv("HEPH_S", raising=False)
    assert reg.str("HEPH_S") == ""


# ── int accessor ──────────────────────────────────────────────────────


def test_int_reads_env(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """int() parses a numeric environment value."""
    monkeypatch.setenv("HEPH_I", "42")
    assert reg.int("HEPH_I") == 42


def test_int_unset_uses_registered_default(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """int() uses the registered default when the var is unset."""
    monkeypatch.delenv("HEPH_I", raising=False)
    reg.register("HEPH_I", type=int, default=99)
    assert reg.int("HEPH_I") == 99


def test_int_unset_unregistered_returns_zero(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """int() returns 0 for an unset, unregistered var."""
    monkeypatch.delenv("HEPH_I", raising=False)
    assert reg.int("HEPH_I") == 0


def test_int_malformed_falls_back_and_warns(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """int() logs a warning and falls back when the value is non-numeric."""
    monkeypatch.setenv("HEPH_I", "not-a-number")
    with caplog.at_level(logging.WARNING):
        assert reg.int("HEPH_I", default=5) == 5
    assert "non-integer" in caplog.text


# ── float accessor ────────────────────────────────────────────────────


def test_float_reads_env(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """float() parses a numeric environment value."""
    monkeypatch.setenv("HEPH_F", "3.14")
    assert reg.float("HEPH_F") == pytest.approx(3.14)


def test_float_unset_unregistered_returns_zero(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """float() returns 0.0 for an unset, unregistered var."""
    monkeypatch.delenv("HEPH_F", raising=False)
    assert reg.float("HEPH_F") == pytest.approx(0.0)


def test_float_malformed_falls_back_and_warns(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """float() logs a warning and falls back when the value is non-numeric."""
    monkeypatch.setenv("HEPH_F", "nope")
    with caplog.at_level(logging.WARNING):
        assert reg.float("HEPH_F", default=1.5) == pytest.approx(1.5)
    assert "non-float" in caplog.text


# ── bool accessor ─────────────────────────────────────────────────────


@pytest.mark.parametrize("raw", ["true", "YES", "On", "1", " true "])
def test_bool_truthy(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """bool() recognises the truthy token set (case/space-insensitive)."""
    monkeypatch.setenv("HEPH_B", raw)
    assert reg.bool("HEPH_B") is True


@pytest.mark.parametrize("raw", ["false", "NO", "Off", "0"])
def test_bool_falsy(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    """bool() recognises the falsy token set (case-insensitive)."""
    monkeypatch.setenv("HEPH_B", raw)
    assert reg.bool("HEPH_B") is False


def test_bool_unset_unregistered_returns_false(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bool() returns False for an unset, unregistered var."""
    monkeypatch.delenv("HEPH_B", raising=False)
    assert reg.bool("HEPH_B") is False


def test_bool_unset_uses_registered_default(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """bool() uses the registered default when the var is unset."""
    monkeypatch.delenv("HEPH_B", raising=False)
    reg.register("HEPH_B", type=bool, default=True)
    assert reg.bool("HEPH_B") is True


def test_bool_malformed_falls_back_and_warns(
    reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """bool() logs a warning and falls back on an unrecognised token."""
    monkeypatch.setenv("HEPH_B", "maybe")
    with caplog.at_level(logging.WARNING):
        assert reg.bool("HEPH_B", default=True) is True
    assert "non-boolean" in caplog.text


# ── Diagnostics helpers ───────────────────────────────────────────────


def test_snapshot_shows_value_or_default(reg: EnvRegistry, monkeypatch: pytest.MonkeyPatch) -> None:
    """snapshot() reports the live value when set, else the default marker."""
    reg.register("HEPH_SET", type=str, default="d1")
    reg.register("HEPH_UNSET", type=int, default=123)
    monkeypatch.setenv("HEPH_SET", "live")
    monkeypatch.delenv("HEPH_UNSET", raising=False)
    snap = reg.snapshot()
    assert snap["HEPH_SET"] == "live"
    assert snap["HEPH_UNSET"] == "<default: 123>"


def test_as_rst_table_empty(reg: EnvRegistry) -> None:
    """as_rst_table() returns an empty string when nothing is registered."""
    assert reg.as_rst_table() == ""


def test_as_rst_table_renders_registered(reg: EnvRegistry) -> None:
    """as_rst_table() emits a list-table row for each registered var."""
    reg.register("HEPH_DOC", type=int, default=10, description="a knob")
    table = reg.as_rst_table()
    assert ".. list-table:: Environment Variables" in table
    assert "``HEPH_DOC``" in table
    assert "int" in table
    assert "a knob" in table


# ── Module singleton pre-registration ─────────────────────────────────


def test_singleton_preregisters_known_vars() -> None:
    """The module-level singleton ships with the well-known vars registered."""
    assert env.spec("HEPH_PLANNER_MODEL") is not None
    assert env.spec("HEPHAESTUS_RATE_GUARD") is not None
    assert env.spec("HEPH_GH_TIMEOUT") is not None


def test_singleton_registered_default_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bundled registered default is returned when the var is unset."""
    monkeypatch.delenv("HEPH_PLANNER_AGENT_TIMEOUT", raising=False)
    assert env.int("HEPH_PLANNER_AGENT_TIMEOUT") == 7200
