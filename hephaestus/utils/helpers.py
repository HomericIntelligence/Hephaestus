"""Helper functions for ProjectHephaestus.

General utility functions that don't fit in other specific modules.
"""
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, Union


def slugify(text: str) -> str:
    """Convert text to a URL-friendly slug.
    
    Args:
        text: Text to convert to slug
        
    Returns:
        URL-friendly slug string
    """
    # Normalize unicode characters
    text = unicodedata.normalize('NFKD', text)
    # Convert to ASCII
    text = text.encode('ascii', 'ignore').decode('ascii')
    # Convert to lowercase and replace spaces/underscores with hyphens
    text = re.sub(r'[\s_]+', '-', text.lower())
    # Remove non-alphanumeric characters (except hyphens)
    text = re.sub(r'[^a-z0-9-]', '', text)
    # Remove leading/trailing hyphens
    text = text.strip('-')
    # Replace multiple consecutive hyphens with single hyphen
    text = re.sub(r'-+', '-', text)
    return text


def human_readable_size(size_bytes: Union[int, float]) -> str:
    """Convert byte size to human readable format.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        Human readable size string with appropriate unit
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


def flatten_dict(d: Dict[str, Any], parent_key: str = '', sep: str = '.') -> Dict[str, Any]:
    """Flatten nested dictionary using dot notation for keys.
    
    Args:
        d: Dictionary to flatten
        parent_key: Parent key prefix
        sep: Separator for nested keys
        
    Returns:
        Flattened dictionary
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)


def get_repo_root(start_path: Union[str, Path, None] = None) -> Path:
    """Find repository root by looking for .git directory.
    
    Args:
        start_path: Starting path to search from. Defaults to current directory.
        
    Returns:
        Path to repository root
        
    Raises:
        FileNotFoundError: If no repository root is found
    """
    if start_path is None:
        start_path = Path.cwd()
    else:
        start_path = Path(start_path).resolve()
    
    path = start_path
    while path != path.parent:  # Stop at filesystem root
        if (path / ".git").exists():
            return path
        path = path.parent
    
    # If we get here, we didn't find a .git directory
    # Return the original start path as fallback
    return start_path
