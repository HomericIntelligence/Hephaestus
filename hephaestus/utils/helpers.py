#!/usr/bin/env python3
"""
General utility functions for ProjectHephaestus.

Common helper functions for file operations, string manipulation,
and system utilities.

Usage:
    from hephaestus.utils.helpers import slugify, retry_with_backoff
    
    name = slugify("My Project Name")
    result = retry_with_backoff(some_unreliable_function)
"""

import time
import re
import functools
import math
from typing import Any, Callable, Optional
from pathlib import Path

# Import slugify from the shared layer within our package
from ..shared.utils.common import slugify


def retry_with_backoff(func: Callable,
                      max_retries: int = 3,
                      base_delay: float = 1.0,
                      exponential_base: float = 2.0) -> Callable:
    """Decorator to retry function with exponential backoff.
    
    Args:
        func: Function to decorate
        max_retries: Maximum number of retries
        base_delay: Base delay in seconds
        exponential_base: Base for exponential backoff calculation
        
    Returns:
        Decorated function
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        retries = 0
        while retries <= max_retries:
            try:
                return func(*args, **kwargs)
            except Exception as e:
                retries += 1
                if retries > max_retries:
                    raise e
                delay = base_delay * (exponential_base ** (retries - 1))
                time.sleep(delay)
        return None
    return wrapper


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human readable string.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        Human readable size string
    """
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    size = float(size_bytes)
    
    while size >= 1024.0 and i < len(size_names) - 1:
        size /= 1024.0
        i += 1
        
    return f"{size:.1f} {size_names[i]}"


def flatten_dict(d: dict, separator: str = '.', prefix: str = '') -> dict:
    """Flatten nested dictionary.
    
    Args:
        d: Dictionary to flatten
        separator: Separator for nested keys
        prefix: Prefix for top-level keys
        
    Returns:
        Flattened dictionary
    """
    items = []
    for k, v in d.items():
        new_key = f"{prefix}{separator}{k}" if prefix else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, separator, new_key).items())
        else:
            items.append((new_key, v))
    return dict(items)
