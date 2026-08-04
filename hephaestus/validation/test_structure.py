"""Backward-compatibility shim for hephaestus.validation.test_layout."""

from hephaestus.validation.test_layout import (
    ALLOWED_ROOT_FILES as ALLOWED_ROOT_FILES,
    SANCTIONED_EXTRA_TEST_DIRS as SANCTIONED_EXTRA_TEST_DIRS,
    check_no_ghost_packages as check_no_ghost_packages,
    check_no_loose_test_files as check_no_loose_test_files,
    check_no_phantom_test_dirs as check_no_phantom_test_dirs,
    check_no_stray_tests_root_files as check_no_stray_tests_root_files,
    check_no_unsanctioned_test_dirs as check_no_unsanctioned_test_dirs,
    check_scripts_coverage as check_scripts_coverage,
    check_test_directory_mirrors as check_test_directory_mirrors,
    check_test_structure as check_test_structure,
    main as main,
)

__all__ = [
    "ALLOWED_ROOT_FILES",
    "SANCTIONED_EXTRA_TEST_DIRS",
    "check_no_ghost_packages",
    "check_no_loose_test_files",
    "check_no_phantom_test_dirs",
    "check_no_stray_tests_root_files",
    "check_no_unsanctioned_test_dirs",
    "check_scripts_coverage",
    "check_test_directory_mirrors",
    "check_test_structure",
    "main",
]
