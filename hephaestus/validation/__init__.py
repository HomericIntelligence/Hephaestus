"""Validation utilities for ProjectHephaestus."""

from hephaestus.validation.audit import (
    filter_audit_results,
    severity_label,
)
from hephaestus.validation.complexity import check_max_complexity
from hephaestus.validation.config_lint import ConfigLinter
from hephaestus.validation.coverage import check_coverage, parse_coverage_report
from hephaestus.validation.doc_config import check_doc_config_consistency
from hephaestus.validation.doc_policy import (
    Finding as DocPolicyFinding,
)
from hephaestus.validation.doc_policy import (
    Severity,
)
from hephaestus.validation.doc_policy import (
    scan_file as scan_doc_policy,
)
from hephaestus.validation.doc_policy import (
    scan_repository as scan_doc_policy_repository,
)
from hephaestus.validation.docstrings import (
    FragmentFinding,
    is_genuine_fragment,
)
from hephaestus.validation.docstrings import (
    scan_file as scan_docstrings,
)
from hephaestus.validation.markdown import (
    ReadmeValidationResult,
    check_markdown_formatting,
    check_required_sections,
    count_markdown_issues,
    extract_markdown_links,
    extract_sections,
    find_markdown_files,
    find_readmes,
    validate_all_links,
    validate_all_readmes,
    validate_directory_exists,
    validate_file_exists,
    validate_file_links,
    validate_internal_link,
    validate_readme,
    validate_relative_link,
)
from hephaestus.validation.mypy_per_file import check_mypy_per_file
from hephaestus.validation.python_version import check_python_version_consistency
from hephaestus.validation.readme_commands import (
    CodeBlock,
    ReadmeValidator,
    ValidationReport,
    ValidationResult,
)
from hephaestus.validation.stale_scripts import check_stale_scripts, find_stale_scripts
from hephaestus.validation.structure import StructureValidator
from hephaestus.validation.test_structure import (
    check_no_loose_test_files,
    check_test_directory_mirrors,
    check_test_structure,
)
from hephaestus.validation.tier_labels import TierLabelFinding
from hephaestus.validation.tier_labels import scan_repository as scan_tier_labels
from hephaestus.validation.type_aliases import detect_shadowing, is_shadowing_pattern

__all__ = [
    "CodeBlock",
    "ConfigLinter",
    "DocPolicyFinding",
    "FragmentFinding",
    "ReadmeValidationResult",
    "ReadmeValidator",
    "Severity",
    "StructureValidator",
    "TierLabelFinding",
    "ValidationReport",
    "ValidationResult",
    "check_coverage",
    "check_doc_config_consistency",
    "check_markdown_formatting",
    "check_max_complexity",
    "check_mypy_per_file",
    "check_no_loose_test_files",
    "check_python_version_consistency",
    "check_required_sections",
    "check_stale_scripts",
    "check_test_directory_mirrors",
    "check_test_structure",
    "count_markdown_issues",
    "detect_shadowing",
    "extract_markdown_links",
    "extract_sections",
    "filter_audit_results",
    "find_markdown_files",
    "find_readmes",
    "find_stale_scripts",
    "is_genuine_fragment",
    "is_shadowing_pattern",
    "parse_coverage_report",
    "scan_doc_policy",
    "scan_doc_policy_repository",
    "scan_docstrings",
    "scan_tier_labels",
    "severity_label",
    "validate_all_links",
    "validate_all_readmes",
    "validate_directory_exists",
    "validate_file_exists",
    "validate_file_links",
    "validate_internal_link",
    "validate_readme",
    "validate_relative_link",
]
