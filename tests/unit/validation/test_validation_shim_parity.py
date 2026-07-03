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

EXPECTED_PUBLIC_NAMES = {
    "hephaestus.validation.docstrings": (
        "FragmentFinding",
        "format_json",
        "format_report",
        "is_genuine_fragment",
        "main",
        "scan_directory",
        "scan_file",
    ),
    "hephaestus.validation.doc_config": (
        "check_addopts_cov_fail_under",
        "check_claude_md_threshold",
        "check_doc_config_consistency",
        "check_dod_threshold",
        "check_readme_cov_path",
        "check_readme_test_count",
        "collect_actual_test_count",
        "extract_cov_fail_under_from_addopts",
        "extract_cov_path",
        "load_coverage_threshold",
        "main",
    ),
    "hephaestus.validation.doc_policy": (
        "EXCLUDED_PREFIXES",
        "Finding",
        "POLICY_RULES",
        "Severity",
        "format_json_report",
        "format_text_report",
        "main",
        "scan_file",
        "scan_repository",
    ),
    "hephaestus.validation.type_aliases": (
        "check_files",
        "detect_shadowing",
        "format_error",
        "is_shadowing_pattern",
        "main",
    ),
    "hephaestus.validation.complexity": (
        "check_max_complexity",
        "main",
        "run_ruff_complexity_check",
    ),
    "hephaestus.validation.mypy_per_file": (
        "check_mypy_per_file",
        "main",
        "run_mypy_per_file",
        "split_flags_and_files",
    ),
    "hephaestus.validation.tier_labels": (
        "BAD_PATTERNS",
        "CANONICAL_TIERS",
        "TierLabelFinding",
        "check_tier_label_consistency",
        "find_violations",
        "format_json",
        "format_report",
        "main",
        "scan_repository",
    ),
    "hephaestus.validation.cli_tier_docs": (
        "TierDocFinding",
        "VALID_TIERS",
        "find_duplicate_tiers",
        "find_violations",
        "format_json",
        "format_report",
        "load_documented_tiers",
        "load_pyproject_scripts",
        "main",
    ),
    "hephaestus.validation.skill_catalog": (
        "check_claude_skill_arguments",
        "check_skill_catalog",
        "check_skill_frontmatter",
        "extract_claude_skill_arguments",
        "extract_skill_table_rows",
        "main",
    ),
    "hephaestus.validation.repo_analyze_skills": (
        "COMMON_DIR",
        "REPO_ROOT",
        "SKILLS_DIR",
        "main",
    ),
    "hephaestus.validation.skill_merge_method": (
        "FENCE",
        "HARDCODED",
        "MARKER",
        "main",
        "scan",
    ),
}


def _public_names(module: object) -> list[str]:
    names = []
    for name in dir(module):
        if name.startswith("_"):
            continue
        obj = getattr(module, name)
        if getattr(getattr(obj, "__class__", None), "__name__", "") == "module":
            continue
        names.append(name)
    return names


@pytest.mark.parametrize("shim_path,canonical_path", list(SHIMS.items()))
def test_shim_reexports_match_canonical(shim_path: str, canonical_path: str) -> None:
    """Every legacy public name on a shim exists and matches its canonical module."""
    shim = importlib.import_module(shim_path)
    canonical = importlib.import_module(canonical_path)
    expected_names = EXPECTED_PUBLIC_NAMES[shim_path]
    assert _public_names(shim) == list(expected_names), (
        f"{shim_path} public surface drifted from the expected legacy exports"
    )
    for name in expected_names:
        obj = getattr(shim, name)
        assert getattr(canonical, name) is obj, f"{shim_path}.{name} drifted from {canonical_path}"
