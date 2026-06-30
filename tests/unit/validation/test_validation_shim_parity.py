"""Full-surface parity tests for the validation backward-compat shims.

Issue #1446 merged ten flat validation modules into the ``docs/``, ``code/``,
``tiers/``, and ``skills/`` subpackages, leaving each original
``hephaestus.validation.<module>`` path as a thin re-export shim. A missing or
drifted re-export only surfaces as an ``AttributeError`` at a future call site,
so these tests assert object identity for every public name on each shim.
"""

from __future__ import annotations

import importlib

import pytest

# shim module path -> canonical subpackage module path
SHIMS = {
    "hephaestus.validation.docstrings": "hephaestus.validation.docs.docstrings",
    "hephaestus.validation.doc_config": "hephaestus.validation.docs.doc_config",
    "hephaestus.validation.doc_policy": "hephaestus.validation.docs.doc_policy",
    "hephaestus.validation.type_aliases": "hephaestus.validation.code.type_aliases",
    "hephaestus.validation.complexity": "hephaestus.validation.code.complexity",
    "hephaestus.validation.mypy_per_file": "hephaestus.validation.code.mypy_per_file",
    "hephaestus.validation.tier_labels": "hephaestus.validation.tiers.tier_labels",
    "hephaestus.validation.cli_tier_docs": "hephaestus.validation.tiers.cli_tier_docs",
    "hephaestus.validation.skill_catalog": "hephaestus.validation.skills.skill_catalog",
    "hephaestus.validation.repo_analyze_skills": (
        "hephaestus.validation.skills.repo_analyze_skills"
    ),
    "hephaestus.validation.skill_merge_method": ("hephaestus.validation.skills.skill_merge_method"),
}


@pytest.mark.parametrize("shim_path,canonical_path", list(SHIMS.items()))
def test_shim_reexports_match_canonical(shim_path: str, canonical_path: str) -> None:
    """Every public name on a shim is the same object on its canonical module."""
    shim = importlib.import_module(shim_path)
    canonical = importlib.import_module(canonical_path)
    for name in (n for n in dir(shim) if not n.startswith("_")):
        obj = getattr(shim, name)
        # Skip imported module objects (sys, ast, yaml) surfaced by dir().
        if getattr(getattr(obj, "__class__", None), "__name__", "") == "module":
            continue
        assert getattr(canonical, name) is obj, f"{shim_path}.{name} drifted from {canonical_path}"
