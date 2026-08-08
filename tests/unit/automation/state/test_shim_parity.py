"""Full-surface parity tests for the backward-compatibility state shims.

Each flat-path shim
(``hephaestus.automation.{planner_state,implementer_state,review_state}``)
must re-export the same object the canonical ``state.*`` module defines, so
existing import sites keep resolving the identical symbol. A missing re-export
only surfaces as an ``AttributeError`` at some future call site; this parity
test catches the drift up front by asserting object identity for every public
name on each shim.
"""

from __future__ import annotations

import importlib

import pytest

# shim module name (under hephaestus.automation) -> canonical module suffix
PARITY = {
    "planner_state": "state.planner",
    "implementer_state": "state.implementer",
    "review_state": "state.review",
}


@pytest.mark.parametrize("shim_name, canon_suffix", PARITY.items())
def test_shim_reexports_match_canonical(shim_name: str, canon_suffix: str) -> None:
    """Every public name on the shim is the same object on the canonical module."""
    shim = importlib.import_module(f"hephaestus.automation.{shim_name}")
    canon = importlib.import_module(f"hephaestus.automation.{canon_suffix}")
    for name in (n for n in dir(shim) if not n.startswith("_")):
        obj = getattr(shim, name)
        # Filter imported module objects (logging, re, ...) out of dir().
        if getattr(type(obj), "__name__", "") == "module":
            continue
        assert getattr(canon, name) is obj, f"{shim_name}.{name} drifted from {canon_suffix}"
