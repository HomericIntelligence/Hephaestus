"""Parity test for shimmed symbols moved to pipeline/admission.py.

Ensures that loop_runner's shim imports match the canonical implementations
in pipeline.admission (pattern from tests/unit/automation/state/test_shim_parity.py).
"""

from __future__ import annotations

import importlib

import pytest

from hephaestus.automation import loop_runner

# Symbols that were moved from loop_runner to pipeline.admission.
SHIMMED_SYMBOLS = [
    "_fetch_planned_files",
    "_filter_open_issues",
    "_parse_planned_files",
    "_select_non_overlapping",
]


@pytest.mark.parametrize("symbol_name", SHIMMED_SYMBOLS)
def test_shim_reexports_match_canonical(symbol_name: str) -> None:
    """loop_runner shim re-exports match the canonical pipeline.admission implementation."""
    # Get the shim from loop_runner
    shim_obj = getattr(loop_runner, symbol_name)

    # Get the canonical from pipeline.admission
    admission = importlib.import_module("hephaestus.automation.pipeline.admission")
    canonical_obj = getattr(admission, symbol_name)

    # They should be the same object (re-export via `as name` binding)
    assert shim_obj is canonical_obj, f"{symbol_name} drifted: shim is not the canonical object"
