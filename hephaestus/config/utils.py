#!/usr/bin/env python3
"""Enhanced configuration management utilities for ProjectHephaestus.

This module provides utilities for loading, validating, and managing
configuration settings across the HomericIntelligence ecosystem with
support for YAML, environment variables, and hierarchical merging.

Usage:
    from hephaestus.config.utils import load_config, get_setting, merge_configs
    config = load_config('config.yaml')
    value = get_setting(config, 'database.host', default='localhost')
"""

import contextlib
import json
import os
from pathlib import Path
from typing import Any, cast

from hephaestus.logging.utils import get_logger

_logger = get_logger(__name__)

_BOOL_TRUTHY: frozenset[str] = frozenset({"true", "yes", "on", "1"})
_BOOL_FALSY: frozenset[str] = frozenset({"false", "no", "off", "0"})

try:
    import yaml

    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False
    _logger.warning("PyYAML not available, YAML config support disabled")


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load configuration from a YAML or JSON file.

    Args:
        config_path: Path to the configuration file

    Returns:
        Dictionary containing configuration settings

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config file format is unsupported

    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        if config_path.suffix.lower() in [".yml", ".yaml"] and YAML_AVAILABLE:
            return cast(dict[str, Any], yaml.safe_load(f) or {})
        elif config_path.suffix.lower() == ".json":
            return cast(dict[str, Any], json.load(f))
        else:
            raise ValueError(f"Unsupported config format: {config_path.suffix}")


def get_setting(config: dict[str, Any], key_path: str, default: Any | None = None) -> Any:
    """Get a configuration setting using dot notation.

    Args:
        config: Configuration dictionary
        key_path: Dot-separated path to setting (e.g., 'database.host')
        default: Default value if setting not found

    Returns:
        Configuration value or default

    """
    keys = key_path.split(".")
    current = config

    try:
        for key in keys:
            current = current[key]
        return current
    except (KeyError, TypeError):
        return default


def validate_config(config: dict[str, Any], schema: dict[str, Any]) -> bool:
    """Validate configuration against a schema.

    Args:
        config: Configuration dictionary
        schema: Schema defining required fields and types

    Returns:
        True if valid, False otherwise

    """
    errors: list[str] = []
    for key, expected_type in schema.items():
        if key not in config:
            errors.append(f"Missing required config key: {key}")
        elif expected_type and not isinstance(config[key], expected_type):
            errors.append(
                f"Config key {key} has wrong type. Expected {expected_type},"
                f" got {type(config[key])}"
            )
    for error in errors:
        _logger.error(error)
    return len(errors) == 0


def merge_configs(*configs: dict[str, Any]) -> dict[str, Any]:
    """Merge multiple configuration dictionaries with priority.

    Later configs override earlier ones.

    Args:
        *configs: Configuration dictionaries in order of priority

    Returns:
        Merged configuration dictionary

    """
    result: dict[str, Any] = {}
    for config in configs:
        if config:
            _deep_merge(result, config)
    return result


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> None:
    """Deep merge two dictionaries."""
    for key, value in override.items():
        if not isinstance(key, str) or not key:
            _logger.warning("_deep_merge: skipping empty or non-string key %r", key)
            continue
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def load_yaml_config(config_path: str | Path) -> dict[str, Any]:
    """Load configuration from a YAML file with validation.

    Args:
        config_path: Path to the YAML configuration file

    Returns:
        Dictionary containing configuration settings

    """
    if not YAML_AVAILABLE:
        raise RuntimeError("PyYAML is required for YAML config support")

    return load_config(config_path)


def merge_with_env(
    config: dict[str, Any],
    prefix: str = "HEPHAESTUS_",
    convert_bools: bool = False,
) -> dict[str, Any]:
    """Merge configuration with environment variables.

    Environment variables with the given prefix are mapped to config keys
    using double-underscore (``__``) as the nesting delimiter.  A single
    underscore is preserved as part of the key name.

    Mapping examples::

        HEPHAESTUS_DATABASE__HOST       → {"database": {"host": <value>}}
        HEPHAESTUS_MAX_CONNECTIONS      → {"max_connections": <value>}
        HEPHAESTUS_DATABASE__MAX_RETRIES → {"database": {"max_retries": <value>}}

    Environment variables that produce empty key segments (e.g., trailing
    or consecutive double-underscores) are skipped with a warning.

    When two environment variables conflict on nesting (e.g.,
    ``HEPHAESTUS_A=scalar`` and ``HEPHAESTUS_A__B=nested``), the
    last variable in sorted order wins and a warning is logged.

    Args:
        config: Base configuration dictionary
        prefix: Environment variable prefix to look for
        convert_bools: If True, convert boolean-like string values
            (true/false/yes/no/on/off/1/0, case-insensitive) to Python
            bool. When enabled, "1" and "0" become True/False instead
            of int. Defaults to False for backward compatibility.

    Returns:
        Configuration merged with environment variables

    """
    env_config: dict[str, Any] = {}

    # Sort env vars for deterministic processing order
    env_vars = sorted(
        ((k, v) for k, v in os.environ.items() if k.startswith(prefix)),
        key=lambda item: item[0],
    )

    for key, value in env_vars:
        # Convert HEPHAESTUS_DATABASE__HOST to database.host
        # Double underscore is the nesting delimiter; single underscore is
        # preserved as part of the key name (e.g. max_connections stays intact).
        config_key = key[len(prefix) :].lower().replace("__", ".")

        # Filter out empty segments from malformed env var names
        keys = [k for k in config_key.split(".") if k]
        if not keys:
            _logger.warning(
                "Skipping malformed env var %r: produces no valid config keys",
                key,
            )
            continue

        # Try to convert to bool, int, or float if possible
        typed_value: int | float | bool | str = value
        lower_value = value.lower()
        if convert_bools and lower_value in _BOOL_TRUTHY:
            typed_value = True
        elif convert_bools and lower_value in _BOOL_FALSY:
            typed_value = False
        else:
            try:
                typed_value = int(value)
            except ValueError:
                with contextlib.suppress(ValueError):
                    typed_value = float(value)

        # Set nested keys
        current = env_config
        for k in keys[:-1]:
            existing = current.get(k)
            if existing is None:
                current[k] = {}
            elif not isinstance(existing, dict):
                _logger.warning(
                    "Environment variable '%s' requires nesting under key '%s', "
                    "which was already set to a scalar value. "
                    "The scalar value is being overwritten by a dict.",
                    key,
                    k,
                )
                current[k] = {}
            current = current[k]

        leaf = keys[-1]
        existing_leaf = current.get(leaf)
        if isinstance(existing_leaf, dict):
            _logger.warning(
                "Environment variable '%s' sets key '%s' to a scalar, "
                "but it was already a nested dict from other environment variables. "
                "The nested dict is being overwritten by the scalar value.",
                key,
                leaf,
            )
        current[leaf] = typed_value

    return merge_configs(config, env_config)


# Example usage function
def get_config_value(
    key_path: str,
    default: Any | None = None,
    config_files: list[str] | None = None,
    convert_bools: bool = False,
) -> Any:
    """High-level function to get a configuration value with full merging.

    Loads defaults, then user config, then environment variables.

    Args:
        key_path: Dot-separated path to setting
        default: Default value if not found
        config_files: List of config files to load in order
        convert_bools: If True, convert boolean-like string values from
            environment variables (true/false/yes/no/on/off/1/0,
            case-insensitive) to Python bool before merging.

    Returns:
        Configuration value or default

    """
    config = {}

    # Load default config if exists
    default_config_path = Path("config/default.yaml")
    if default_config_path.exists() and YAML_AVAILABLE:
        config = load_config(default_config_path)

    # Load user configs
    if config_files:
        for config_file in config_files:
            if Path(config_file).exists():
                user_config = load_config(config_file)
                config = merge_configs(config, user_config)

    # Merge with environment
    config = merge_with_env(config, convert_bools=convert_bools)

    # Get the specific value
    return get_setting(config, key_path, default)
