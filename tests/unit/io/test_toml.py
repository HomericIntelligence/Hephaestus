"""Tests for hephaestus.io.toml's Python 3.13 tomllib accessor."""

from __future__ import annotations

import tomllib

from hephaestus.io.toml import import_tomllib


def test_returns_a_toml_module() -> None:
    """On a supported interpreter a module exposing ``load`` is returned."""
    module = import_tomllib()
    assert module is not None
    assert hasattr(module, "load")
    assert hasattr(module, "loads")


def test_real_module_can_parse() -> None:
    """The resolved module round-trips a trivial TOML document."""
    module = import_tomllib()
    assert module is not None
    data = module.loads('[tool]\nname = "x"\n')
    assert data == {"tool": {"name": "x"}}


def test_resolver_matches_standard_library_tomllib() -> None:
    """Resolver returns the standard-library TOML module."""
    assert import_tomllib() is tomllib
