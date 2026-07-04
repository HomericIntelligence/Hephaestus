"""Public surface tests for flat validation modules.

Issue #1736 removes the nested ``validation/docs``, ``validation/code``,
``validation/tiers``, and ``validation/skills`` packages. The retained flat
modules must keep their public names importable for console scripts and tests.
"""

from __future__ import annotations

import importlib

import pytest

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


@pytest.mark.parametrize("module_path", list(EXPECTED_PUBLIC_NAMES))
def test_flat_validation_module_public_surface(module_path: str) -> None:
    """Every retained flat validation module exposes its expected public names."""
    module = importlib.import_module(module_path)
    expected_names = EXPECTED_PUBLIC_NAMES[module_path]
    missing = [name for name in expected_names if not hasattr(module, name)]
    assert missing == [], f"{module_path} is missing expected exports: {missing}"
