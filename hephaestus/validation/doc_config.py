"""Backward-compatibility shim.

Canonical implementation: :mod:`hephaestus.validation.docs.doc_config`.
Retained so existing import sites and the ``hephaestus-check-doc-config``
console script need no changes.
"""

import sys

from hephaestus.validation.docs.doc_config import (
    check_addopts_cov_fail_under as check_addopts_cov_fail_under,
    check_claude_md_threshold as check_claude_md_threshold,
    check_doc_config_consistency as check_doc_config_consistency,
    check_dod_threshold as check_dod_threshold,
    check_readme_cov_path as check_readme_cov_path,
    check_readme_test_count as check_readme_test_count,
    collect_actual_test_count as collect_actual_test_count,
    extract_cov_fail_under_from_addopts as extract_cov_fail_under_from_addopts,
    extract_cov_path as extract_cov_path,
    load_coverage_threshold as load_coverage_threshold,
    main as main,
)

__all__ = [
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
]


if __name__ == "__main__":
    sys.exit(main())
