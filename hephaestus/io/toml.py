"""Shared TOML-module resolution for the ``tomllib`` / ``tomli`` fallback.

``tomllib`` ships in the standard library on Python 3.11+. On Python 3.10 it is
absent, and callers fall back to the ``tomli`` backport. Several modules across
the package reimplemented the identical import-fallback loop; this module
provides a single resolver so that logic lives in one place.

The resolver returns the imported module (so callers keep using
``module.load(fh)`` exactly as before) or ``None`` when neither package is
installed — preserving each caller's existing ``if module is None`` fallback
behaviour.

Usage::

    from hephaestus.io.toml import import_tomllib

    _tomllib = import_tomllib()
    if _tomllib is not None:
        with path.open("rb") as fh:
            data = _tomllib.load(fh)
"""

from __future__ import annotations

import importlib
import types


def import_tomllib() -> types.ModuleType | None:
    """Return the ``tomllib`` module, the ``tomli`` backport, or ``None``.

    Tries ``tomllib`` (stdlib on Python 3.11+) first, then the ``tomli``
    backport (used on Python 3.10). Returns ``None`` if neither is importable so
    that callers can fall back to a regex/manual parser or a default config.

    Returns:
        The resolved TOML module exposing ``load`` / ``loads``, or ``None`` if
        no TOML parser is available.

    """
    for mod_name in ("tomllib", "tomli"):
        try:
            return importlib.import_module(mod_name)
        except ModuleNotFoundError:
            continue
    return None
