"""Documentation quality validators.

Merges the docstring-fragment, doc-config-consistency, and doc-policy checks
into one focused subpackage. The original ``hephaestus.validation.<module>``
paths remain as thin re-export shims for backward compatibility.
"""

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
)
from hephaestus.validation.docs.doc_policy import (
    Finding as Finding,
    Severity as Severity,
    format_json_report as format_json_report,
    format_text_report as format_text_report,
    scan_repository as scan_repository,
)
from hephaestus.validation.docs.docstrings import (
    FragmentFinding as FragmentFinding,
    format_json as format_json,
    format_report as format_report,
    is_genuine_fragment as is_genuine_fragment,
    scan_directory as scan_directory,
    scan_file as scan_file,
)

__all__ = [
    "Finding",
    "FragmentFinding",
    "Severity",
    "check_addopts_cov_fail_under",
    "check_claude_md_threshold",
    "check_doc_config_consistency",
    "check_dod_threshold",
    "check_readme_cov_path",
    "check_readme_test_count",
    "collect_actual_test_count",
    "extract_cov_fail_under_from_addopts",
    "extract_cov_path",
    "format_json",
    "format_json_report",
    "format_report",
    "format_text_report",
    "is_genuine_fragment",
    "load_coverage_threshold",
    "scan_directory",
    "scan_file",
    "scan_repository",
]
