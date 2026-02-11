#!/usr/bin/env python3
"""
Configuration management utilities for ProjectHephaestus.

This module provides utilities for loading, validating, and managing 
configuration settings across the HomericIntelligence ecosystem.

Usage:
    from hephaestus.config.utils import load_config, get_setting
    
    config = load_config('config.yaml')
    value = get_setting(config, 'database.host', default='localhost')
"""

import os
import sys
from typing import Any, Dict, Optional, Union
from pathlib import Path

def load_config(config_path: Union[str, Path]) -> Dict[str, Any]:
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
    
    # Placeholder for actual implementation
    # Would integrate with PyYAML or similar libraries
    return {}

def get_setting(config: Dict[str, Any], key_path: str, 
                default: Optional[Any] = None) -> Any:
    """Get a configuration setting using dot notation.
    
    Args:
        config: Configuration dictionary
        key_path: Dot-separated path to setting (e.g., 'database.host')
        default: Default value if setting not found
        
    Returns:
        Configuration value or default
    """
    keys = key_path.split('.')
    current = config
    
    try:
        for key in keys:
            current = current[key]
        return current
    except (KeyError, TypeError):
        return default

def validate_config(config: Dict[str, Any], 
                   schema: Dict[str, Any]) -> bool:
    """Validate configuration against a schema.
    
    Args:
        config: Configuration dictionary
        schema: Schema defining required fields and types
        
    Returns:
        True if valid, False otherwise
    """
    # Placeholder for actual validation implementation
    return True
