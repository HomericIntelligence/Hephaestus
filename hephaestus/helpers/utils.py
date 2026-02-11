#!/usr/bin/env python3
"""
General utility functions for ProjectHephaestus.

These utilities provide common functionality used across the HomericIntelligence ecosystem.
"""

import time
import re
import math
import random
from typing import Any, Callable, Dict, Union
from pathlib import Path


# Import core utilities from shared layer within our package
from ..shared.utils.common import slugify, load_config, get_nested_value


def retry_with_backoff(func: Callable, 
                      max_retries: int = 3, 
                      base_delay: float = 1.0,
                      max_delay: float = 60.0) -> Any:
    """Retry a function with exponential backoff.
    
    Args:
        func: Function to retry
        max_retries: Maximum number of retry attempts
        base_delay: Base delay in seconds
        max_delay: Maximum delay between retries
        
    Returns:
        Result of successful function call
        
    Raises:
        Exception: Last exception raised if all retries fail
    """
    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            if attempt < max_retries:
                # Calculate delay with exponential backoff and jitter
                delay = min(base_delay * (2 ** attempt), max_delay)
                # Add jitter (±25%)
                jitter = random.uniform(-0.25 * delay, 0.25 * delay)
                time.sleep(delay + jitter)
            else:
                break
    
    raise last_exception


def human_readable_size(size_bytes: int) -> str:
    """Convert bytes to human readable size string.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        Human readable size string (e.g., "1.5 KB")
    """
    if size_bytes == 0:
        return "0 B"
    
    size_names = ["B", "KB", "MB", "GB", "TB"]
    i = int(math.floor(math.log(size_bytes, 1024)))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s} {size_names[i]}"


def flatten_dict(d: Dict[str, Any], 
                parent_key: str = '', 
                sep: str = '.') -> Dict[str, Any]:
    """Flatten a nested dictionary.
    
    Args:
        d: Dictionary to flatten
        parent_key: Parent key prefix
        sep: Separator for nested keys
        
    Returns:
        Flattened dictionary
        
    Examples:
        >>> flatten_dict({'a': {'b': 1, 'c': 2}, 'd': 3})
        {'a.b': 1, 'a.c': 2, 'd': 3}
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)
