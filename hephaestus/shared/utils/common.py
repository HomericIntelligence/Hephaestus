#!/usr/bin/env python3
"""
Shared utility functions.

Consolidated from various project shared utilities.
"""

import json
import yaml
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional


def slugify(text: str) -> str:
    """Convert text to URL-friendly slug.
    
    Args:
        text: Text to convert to slug
        
    Returns:
        URL-friendly slug string
        
    Examples:
        >>> slugify("Hello World!")
        'hello-world'
        >>> slugify("Test@#$%Title")
        'test-title'
    """
    # Convert to lowercase and replace spaces/whitespace with hyphens
    slug = re.sub(r'\s+', '-', text.lower().strip())
    # Remove non-alphanumeric characters except hyphens
    slug = re.sub(r'[^a-z0-9-]', '', slug)
    # Replace multiple hyphens with single hyphen
    slug = re.sub(r'-+', '-', slug)
    # Remove leading/trailing hyphens
    slug = slug.strip('-')
    return slug


def load_config(config_path: str) -> Dict[str, Any]:
    """Load configuration from JSON or YAML file."""
    config_path = Path(config_path)
    
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r') as f:
        if config_path.suffix.lower() in ['.yaml', '.yml']:
            return yaml.safe_load(f)
        elif config_path.suffix.lower() == '.json':
            return json.load(f)
        else:
            # Try to parse as JSON first, fallback to YAML
            content = f.read()
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                return yaml.safe_load(content)


def get_nested_value(data: dict, key_path: str, default=None) -> Any:
    """Get nested dictionary value using dot notation.
    
    Args:
        data: Dictionary to search
        key_path: Dot-separated key path (e.g., "database.host")
        default: Default value if key not found
        
    Returns:
        Value at key path or default
    """
    keys = key_path.split('.')
    current = data
    
    try:
        for key in keys:
            current = current[key]
        return current
    except (KeyError, TypeError):
        return default
