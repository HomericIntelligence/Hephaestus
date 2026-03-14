#!/usr/bin/env python3
"""Repository structure validation utilities.

Validates directory structure, required files, and subdirectories for HomericIntelligence projects.
"""

from pathlib import Path
from typing import Any

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


class StructureValidator:
    """Validates repository directory structure."""

    def __init__(
        self,
        required_directories: list[str],
        required_files: dict[str, list[str]],
        required_subdirs: dict[str, list[str]],
    ):
        """Initialize structure validator.

        Args:
            required_directories: List of required top-level directory names
            required_files: Dict mapping directory names to required files
            required_subdirs: Dict mapping parent dirs to required subdirectories

        """
        self.required_directories = required_directories
        self.required_files = required_files
        self.required_subdirs = required_subdirs

    def check_directory_exists(self, base_path: Path, dir_name: str) -> tuple[bool, str]:
        """Check if a directory exists.

        Args:
            base_path: Base path to check from
            dir_name: Directory name to check

        Returns:
            Tuple of (exists, message)

        """
        dir_path = base_path / dir_name
        if not dir_path.exists():
            return False, f"Missing directory: {dir_name}/"
        if not dir_path.is_dir():
            return False, f"Not a directory: {dir_name}"
        return True, f"✓ {dir_name}/"

    def check_file_exists(self, base_path: Path, dir_name: str, file_name: str) -> tuple[bool, str]:
        """Check if a required file exists.

        Args:
            base_path: Base path to check from
            dir_name: Directory containing the file
            file_name: File name to check

        Returns:
            Tuple of (exists, message)

        """
        file_path = base_path / dir_name / file_name
        if not file_path.exists():
            return False, f"Missing file: {dir_name}/{file_name}"
        if not file_path.is_file():
            return False, f"Not a file: {dir_name}/{file_name}"
        return True, f"✓ {dir_name}/{file_name}"

    def check_subdirectory_exists(
        self, base_path: Path, parent_dir: str, subdir: str
    ) -> tuple[bool, str]:
        """Check if a required subdirectory exists.

        Args:
            base_path: Base path to check from
            parent_dir: Parent directory name
            subdir: Subdirectory name to check

        Returns:
            Tuple of (exists, message)

        """
        subdir_path = base_path / parent_dir / subdir
        if not subdir_path.exists():
            return False, f"Missing subdirectory: {parent_dir}/{subdir}/"
        if not subdir_path.is_dir():
            return False, f"Not a directory: {parent_dir}/{subdir}"
        return True, f"✓ {parent_dir}/{subdir}/"

    def validate_structure(self, repo_root: Path, verbose: bool = False) -> dict[str, list[str]]:
        """Validate repository directory structure.

        Args:
            repo_root: Path to repository root
            verbose: Enable verbose output

        Returns:
            Dictionary with 'passed' and 'failed' lists of validation messages

        """
        results: dict[str, Any] = {"passed": [], "failed": []}

        logger.info("Validating repository directory structure...\n")

        # Check required top-level directories
        logger.info("Checking top-level directories...")
        for dir_name in self.required_directories:
            passed, message = self.check_directory_exists(repo_root, dir_name)
            if passed:
                results["passed"].append(message)
                logger.info(f"  {message}") if verbose else None
            else:
                results["failed"].append(message)
                logger.error(f"  ✗ {message}")

        # Check required files
        logger.info("\nChecking required files...")
        for dir_name, files in self.required_files.items():
            for file_name in files:
                passed, message = self.check_file_exists(repo_root, dir_name, file_name)
                if passed:
                    results["passed"].append(message)
                    logger.info(f"  {message}") if verbose else None
                else:
                    results["failed"].append(message)
                    logger.error(f"  ✗ {message}")

        # Check required subdirectories
        logger.info("\nChecking required subdirectories...")
        for parent_dir, subdirs in self.required_subdirs.items():
            for subdir in subdirs:
                passed, message = self.check_subdirectory_exists(repo_root, parent_dir, subdir)
                if passed:
                    results["passed"].append(message)
                    logger.info(f"  {message}") if verbose else None
                else:
                    results["failed"].append(message)
                    logger.error(f"  ✗ {message}")

        return results

    def print_summary(self, results: dict[str, list[str]]) -> None:
        """Print validation summary.

        Args:
            results: Validation results dictionary

        """
        total_checks = len(results["passed"]) + len(results["failed"])
        passed = len(results["passed"])
        failed = len(results["failed"])

        logger.info("\n" + "=" * 70)
        logger.info("STRUCTURE VALIDATION SUMMARY")
        logger.info("=" * 70)
        logger.info(f"Total checks: {total_checks}")
        logger.info(f"Passed: {passed}")
        logger.info(f"Failed: {failed}")

        if failed > 0:
            logger.info(f"\nFailed checks ({failed}):")
            for failure in results["failed"]:
                logger.info(f"  {failure}")

        logger.info("=" * 70)
