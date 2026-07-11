"""Regression tests for removed top-level deprecated symbols.

Guards issue #1599: if a removed deprecated symbol is accidentally added back to
``hephaestus._LAZY_IMPORTS``, package import must fail loudly instead of
silently re-exposing the symbol through the top-level lazy loader.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest

import hephaestus


@pytest.mark.parametrize("symbol", ("get_config_value", "retry_with_jitter"))
def test_removed_top_level_symbols_not_in_lazy_imports(symbol: str) -> None:
    """Removed deprecated top-level symbols must stay out of the lazy surface."""
    assert symbol not in hephaestus._LAZY_IMPORTS


def test_removed_top_level_symbols_guard_matches_lazy_import_surface() -> None:
    """Removed-symbol replacement keys must stay disjoint from the lazy import map."""
    removed_symbols = set(hephaestus._REMOVED_TOP_LEVEL_SYMBOL_REPLACEMENTS)
    assert removed_symbols.isdisjoint(hephaestus._LAZY_IMPORTS)


@pytest.mark.parametrize("symbol", ("get_config_value", "retry_with_jitter"))
def test_import_fails_if_removed_symbol_is_reintroduced_to_lazy_imports(
    tmp_path: Path, symbol: str
) -> None:
    """Import must fail loudly if a removed symbol is re-added to ``_LAZY_IMPORTS``."""
    init_src = Path(hephaestus.__file__).read_text(encoding="utf-8")
    injected = (
        '    "filter_audit_results": ("hephaestus.validation.audit", "filter_audit_results"),\n'
        f'    "{symbol}": ("hephaestus.utils", "{symbol}"),\n'
        "}"
    )
    mutated = init_src.replace(
        '    "filter_audit_results": ("hephaestus.validation.audit", "filter_audit_results"),\n}',
        injected,
    )
    # Anchor-freshness guard: if _LAZY_IMPORTS gains a new trailing entry the
    # replace() above silently no-ops and this test would pass vacuously.
    assert mutated != init_src, (
        "mutation anchor is stale — update the injected tail to match the "
        "current last entry of _LAZY_IMPORTS in hephaestus/__init__.py"
    )
    module_path = tmp_path / "hephaestus_init_guard_probe.py"
    module_path.write_text(mutated, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(
        f"_hephaestus_init_guard_probe_{uuid.uuid4().hex}", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(
        RuntimeError,
        match=(
            "Removed top-level deprecated symbols must not be present in "
            rf"_LAZY_IMPORTS: {symbol}"
        ),
    ):
        spec.loader.exec_module(module)
