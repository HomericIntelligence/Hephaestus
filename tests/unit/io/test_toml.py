"""Tests for hephaestus.io.toml shared tomllib/tomli resolver."""

from __future__ import annotations

import importlib
import types

import pytest

from hephaestus.io.toml import import_tomllib


def test_returns_a_toml_module() -> None:
    """On a supported interpreter a module exposing ``load`` is returned."""
    module = import_tomllib()
    assert module is not None
    assert hasattr(module, "load")
    assert hasattr(module, "loads")


def test_prefers_tomllib_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """``tomllib`` is tried before the ``tomli`` backport."""
    requested: list[str] = []
    sentinel = types.ModuleType("fake_tomllib")

    def fake_import(name: str) -> types.ModuleType:
        requested.append(name)
        return sentinel

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert import_tomllib() is sentinel
    assert requested == ["tomllib"]


def test_falls_back_to_tomli(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``tomllib`` is absent the ``tomli`` backport is used."""
    requested: list[str] = []
    tomli_stub = types.ModuleType("fake_tomli")

    def fake_import(name: str) -> types.ModuleType:
        requested.append(name)
        if name == "tomllib":
            raise ModuleNotFoundError("no tomllib")
        return tomli_stub

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert import_tomllib() is tomli_stub
    assert requested == ["tomllib", "tomli"]


def test_returns_none_when_neither_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither package imports, ``None`` is returned (3.10 no-tomli case)."""

    def fake_import(name: str) -> types.ModuleType:
        raise ModuleNotFoundError(f"no {name}")

    monkeypatch.setattr(importlib, "import_module", fake_import)
    assert import_tomllib() is None


def test_real_module_can_parse() -> None:
    """The resolved module round-trips a trivial TOML document."""
    module = import_tomllib()
    assert module is not None
    data = module.loads('[tool]\nname = "x"\n')
    assert data == {"tool": {"name": "x"}}


def test_resolver_matches_direct_tomllib_import() -> None:
    """Resolver returns the same module a direct import would yield (3.11+)."""
    try:
        direct = importlib.import_module("tomllib")
    except ModuleNotFoundError:
        pytest.skip("tomllib not present on this interpreter")
    assert import_tomllib() is direct
