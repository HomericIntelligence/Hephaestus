"""Regression tests for top-level deprecated lazy import wiring."""

from __future__ import annotations

import warnings


def test_deprecated_lazy_keys_have_lazy_import_targets() -> None:
    """Every deprecated lazy symbol must still be resolvable by ``__getattr__``."""
    import hephaestus

    missing = set(hephaestus._DEPRECATED_LAZY) - set(hephaestus._LAZY_IMPORTS)
    assert not missing, f"deprecated lazy symbols missing lazy imports: {sorted(missing)}"


def test_deprecated_lazy_access_warns_before_resolving() -> None:
    """Deprecated lazy entries warn at access time without requiring call-time use."""
    import hephaestus

    sentinel = object()
    cached = hephaestus.__dict__.pop("slugify", sentinel)
    hephaestus._DEPRECATED_LAZY["slugify"] = "hephaestus.slugify is deprecated for this test"
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            symbol = hephaestus.slugify
    finally:
        hephaestus._DEPRECATED_LAZY.pop("slugify", None)
        hephaestus.__dict__.pop("slugify", None)
        if cached is not sentinel:
            hephaestus.__dict__["slugify"] = cached

    assert callable(symbol)
    access_warnings = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert access_warnings
    assert "hephaestus.slugify" in str(access_warnings[0].message)
