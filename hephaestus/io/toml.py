"""Shared access to Python 3.13's standard-library :mod:`tomllib` module.

Several modules parse project TOML. Keeping the import in one place makes that
dependency explicit while preserving their existing ``module.load(fh)`` calls.

Usage::

    from hephaestus.io.toml import import_tomllib

    _tomllib = import_tomllib()
    if _tomllib is not None:
        with path.open("rb") as fh:
            data = _tomllib.load(fh)
"""

from __future__ import annotations

import tomllib
import types


def import_tomllib() -> types.ModuleType:
    """Return Python 3.13's standard-library :mod:`tomllib` module.

    Returns:
        The TOML module exposing ``load`` and ``loads``.

    """
    return tomllib
