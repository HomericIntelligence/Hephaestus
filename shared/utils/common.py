#!/usr/bin/env python3
"""
Shared utility functions.

Consolidated from various project shared utilities.
"""

import json
import yaml
import os
from pathlib import Path
from typing import Any, Dict, Optional

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

def slugify(text: str) -> str:
    """Convert text to URL-friendly slug."""
    import re
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[-\s]+', '-', text)
    return text.strip('-')

# More utility functions would be added here based on what we find in the actual projects
