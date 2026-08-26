#!/usr/bin/env python3
"""Enhanced configuration management utilities for Hephaestus.

This module provides utilities for loading, validating, and managing
configuration settings across the HomericIntelligence ecosystem with
support for YAML, JSON, validation, and hierarchical merging.

Usage:
    from hephaestus.config.utils import load_config, get_setting, merge_configs
    config = load_config('config.yaml')
    value = get_setting(config, 'database.host', default='localhost')
"""

import json
from pathlib import Path
from typing import Any, cast

from hephaestus.io.yaml import import_yaml
from hephaestus.logging.utils import get_logger

_logger = get_logger(__name__)


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load configuration from a YAML or JSON file.

    Args:
        config_path: Path to the configuration file

    Returns:
        Dictionary containing configuration settings

    Raises:
        FileNotFoundError: If config file doesn't exist
        ValueError: If config file format is unsupported (e.g. .toml)
        RuntimeError: If a .yaml/.yml file is given but PyYAML is unavailable

    """
    config_path = Path(config_path)

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    suffix = config_path.suffix.lower()
    with open(config_path) as f:
        if suffix in (".yml", ".yaml"):
            yaml = import_yaml()
            return cast(dict[str, Any], yaml.safe_load(f) or {})
        elif suffix == ".json":
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
    keys = [k for k in key_path.split(".") if k]
    if not keys:
        _logger.warning(
            "get_setting: malformed key_path %r (no valid segments); returning default", key_path
        )
        return default
    if len(keys) != len(key_path.split(".")):
        _logger.warning(
            "get_setting: key_path %r has empty segments; interpreting as %r",
            key_path,
            ".".join(keys),
        )
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


def merge_configs(*configs: dict[str, Any] | None) -> dict[str, Any]:
    """Merge multiple configuration dictionaries with priority.

    Later configs override earlier ones.  ``None`` entries are silently skipped.

    Args:
        *configs: Configuration dictionaries in order of priority; None is ignored.

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

    Raises:
        RuntimeError: If PyYAML is unavailable.

    """
    import_yaml()
    return load_config(config_path)
