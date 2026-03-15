#!/usr/bin/env python3
"""Configuration linting utilities for HomericIntelligence projects.

Validates YAML configuration files for syntax, formatting, and common issues.
"""

import re
from pathlib import Path
from typing import Any

from hephaestus.logging.utils import get_logger

logger = get_logger(__name__)


class ConfigLinter:
    """Lints YAML configuration files."""

    def __init__(
        self,
        verbose: bool = False,
        deprecated_keys: dict[str, str] | None = None,
        required_keys: dict[str, list[str]] | None = None,
        perf_thresholds: dict[str, tuple[float, float]] | None = None,
    ):
        """Initialize the linter.

        Args:
            verbose: Enable verbose output
            deprecated_keys: Dict mapping deprecated keys to their replacements
            required_keys: Dict mapping config types to required key lists
            perf_thresholds: Dict mapping parameter names to (min, max) thresholds

        """
        self.verbose = verbose
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.suggestions: list[str] = []

        # Default deprecated keys (callers should pass ML-specific values)
        self.deprecated_keys = deprecated_keys if deprecated_keys is not None else {}

        # Default required keys (callers should pass domain-specific values)
        self.required_keys = required_keys if required_keys is not None else {}

        # Default performance thresholds (callers should pass domain-specific values)
        self.perf_thresholds = perf_thresholds if perf_thresholds is not None else {}

    def lint_file(self, filepath: Path) -> bool:
        """Lint a single configuration file.

        Args:
            filepath: Path to YAML file

        Returns:
            True if file passes linting, False otherwise

        """
        self.errors = []
        self.warnings = []
        self.suggestions = []

        if not filepath.exists():
            self.errors.append(f"File not found: {filepath}")
            return False

        if self.verbose:
            logger.info("Linting: %s", filepath)

        try:
            with open(filepath) as f:
                content = f.read()
        except OSError as e:
            self.errors.append(f"Failed to read file: {e}")
            return False

        # Check YAML syntax
        if not self._check_yaml_syntax(content, filepath):
            return False

        # Check formatting
        self._check_formatting(content, filepath)

        # Parse configuration
        config = self._parse_yaml(content)
        if config is None:
            return False

        # Check for issues
        self._check_deprecated_keys(config, filepath)
        self._check_required_keys(config, filepath)
        self._check_duplicate_values(config, filepath)
        self._check_performance(config, filepath)

        return len(self.errors) == 0

    def _check_yaml_syntax(self, content: str, filepath: Path) -> bool:
        """Check if YAML syntax is valid.

        Args:
            content: File content
            filepath: Path to file

        Returns:
            True if syntax is valid

        """
        try:
            lines = content.split("\n")
            brace_count = 0
            bracket_count = 0

            for i, line in enumerate(lines):
                # Skip comments
                stripped = line.split("#")[0]

                # Count braces and brackets
                brace_count += stripped.count("{") - stripped.count("}")
                bracket_count += stripped.count("[") - stripped.count("]")

                # Check for common issues
                # Not a URL: skip lines with "://"
                if (
                    ":" in stripped
                    and not re.match(r"^\s*[\w\-]+:", stripped)
                    and "://" not in stripped
                ):
                    self.warnings.append(f"{filepath}:{i + 1} - Possible malformed key")

            if brace_count != 0:
                self.errors.append(f"{filepath} - Unmatched braces")
                return False

            if bracket_count != 0:
                self.errors.append(f"{filepath} - Unmatched brackets")
                return False

            return True

        except (ValueError, TypeError) as e:
            self.errors.append(f"Syntax check failed: {e}")
            return False

    def _check_formatting(self, content: str, filepath: Path) -> None:
        """Check formatting issues.

        Args:
            content: File content
            filepath: Path to file

        """
        lines = content.split("\n")

        for i, line in enumerate(lines):
            # Check for tabs
            if "\t" in line:
                self.warnings.append(f"{filepath}:{i + 1} - Use spaces instead of tabs")

            # Check for trailing whitespace
            if line != line.rstrip():
                self.suggestions.append(f"{filepath}:{i + 1} - Trailing whitespace")

            # Check for inconsistent indentation
            if line and line[0] == " ":
                indent = len(line) - len(line.lstrip())
                if indent % 2 != 0:
                    self.warnings.append(
                        f"{filepath}:{i + 1} - Inconsistent indentation (use 2 spaces)"
                    )

    def _parse_yaml(self, content: str) -> dict[str, Any] | None:
        """Parse YAML content.

        Args:
            content: YAML content string

        Returns:
            Parsed configuration dict or None if parsing fails

        """
        try:
            import yaml

            result = yaml.safe_load(content)
            return result if isinstance(result, dict) else {}
        except ImportError:
            logger.warning("PyYAML not installed, skipping YAML parsing checks")
            return {}
        except Exception as e:  # broad catch intentional: yaml has undocumented exception subtypes
            self.errors.append(f"YAML parsing failed: {e}")
            return None

    def _check_deprecated_keys(self, config: dict[str, Any], filepath: Path) -> None:
        """Check for deprecated configuration keys.

        Args:
            config: Configuration dictionary
            filepath: Path to file

        """
        for deprecated_key, replacement in self.deprecated_keys.items():
            if "." in deprecated_key:
                # Handle nested keys
                parts = deprecated_key.split(".")
                current = config
                for part in parts:
                    if isinstance(current, dict) and part in current:
                        current = current[part]
                    else:
                        break
                else:
                    self.warnings.append(
                        f"{filepath} - Deprecated key '{deprecated_key}', "
                        f"use '{replacement}' instead"
                    )
            elif deprecated_key in config:
                self.warnings.append(
                    f"{filepath} - Deprecated key '{deprecated_key}', use '{replacement}' instead"
                )

    def _check_required_keys(self, config: dict[str, Any], filepath: Path) -> None:
        """Check for required configuration keys.

        Args:
            config: Configuration dictionary
            filepath: Path to file

        """
        # Try to detect config type from filename or content
        filename = filepath.stem
        config_type = None

        for key_type in self.required_keys:
            if key_type in filename or key_type in config:
                config_type = key_type
                break

        if config_type and config_type in self.required_keys:
            for required_key in self.required_keys[config_type]:
                if required_key not in config:
                    self.errors.append(f"{filepath} - Missing required key '{required_key}'")

    def _check_duplicate_values(self, config: dict[str, Any], filepath: Path) -> None:
        """Check for duplicate values in configuration.

        Args:
            config: Configuration dictionary
            filepath: Path to file

        """
        # Flatten config and check for duplicate values
        values = []

        def flatten(d: dict[str, Any], parent_key: str = "") -> None:
            for k, v in d.items():
                new_key = f"{parent_key}.{k}" if parent_key else k
                if isinstance(v, dict):
                    flatten(v, new_key)
                else:
                    values.append((new_key, v))

        flatten(config)

        # Check for duplicate values (may indicate copy-paste errors)
        seen_values: dict[Any, str] = {}
        for key, value in values:
            if isinstance(value, (int, float, str)) and value != "":
                if value in seen_values:
                    self.suggestions.append(
                        f"{filepath} - Duplicate value '{value}' "
                        f"in '{key}' and '{seen_values[value]}'"
                    )
                else:
                    seen_values[value] = key

    def _check_performance(self, config: dict[str, Any], filepath: Path) -> None:
        """Check performance-related settings.

        Args:
            config: Configuration dictionary
            filepath: Path to file

        """
        for param, (min_val, max_val) in self.perf_thresholds.items():
            if param in config:
                value = config[param]
                if isinstance(value, (int, float)) and (value < min_val or value > max_val):
                    self.warnings.append(
                        f"{filepath} - '{param}' value {value} "
                        f"outside recommended range ({min_val}-{max_val})"
                    )

    def print_results(self) -> None:
        """Print linting results."""
        if self.errors:
            logger.error("\n%d error(s) found:", len(self.errors))
            for error in self.errors:
                logger.error("  ✗ %s", error)

        if self.warnings:
            logger.warning("\n%d warning(s) found:", len(self.warnings))
            for warning in self.warnings:
                logger.warning("  ⚠ %s", warning)

        if self.suggestions:
            logger.info("\n%d suggestion(s):", len(self.suggestions))
            for suggestion in self.suggestions:
                logger.info("  ℹ %s", suggestion)

        if not self.errors and not self.warnings and not self.suggestions:
            logger.info("✓ No issues found")
