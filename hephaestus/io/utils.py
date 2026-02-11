#!/usr/bin/env python3
"""
Input/output utilities for ProjectHephaestus.

Standardized interfaces for file operations, data serialization,
and resource management.

Usage:
    from hephaestus.io.utils import safe_write, ensure_directory
    
    ensure_directory('/path/to/dir')
    safe_write('/path/to/file.txt', 'content')
"""

import os
import json
import yaml
import pickle
from typing import Any, Union, Optional
from pathlib import Path


def read_file(filepath: Union[str, Path], mode: str = 'r') -> Union[str, bytes]:
    """Read content from a file.
    
    Args:
        filepath: Path to file
        mode: File open mode ('r' for text, 'rb' for binary)
        
    Returns:
        File content as string or bytes
        
    Raises:
        FileNotFoundError: If file doesn't exist
        IOError: If file cannot be read
    """
    filepath = Path(filepath)
    with open(filepath, mode) as f:
        return f.read()


def write_file(filepath: Union[str, Path], 
               content: Union[str, bytes],
               mode: str = 'w') -> bool:
    """Write content to a file.
    
    Args:
        filepath: Path to file
        content: Content to write
        mode: File open mode ('w' for text, 'wb' for binary)
        
    Returns:
        True if successful, False otherwise
    """
    filepath = Path(filepath)
    try:
        # Ensure parent directory exists
        filepath.parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, mode) as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Failed to write to {filepath}: {e}")
        return False


def ensure_directory(path: Union[str, Path]) -> bool:
    """Ensure directory exists, creating it if necessary.
    
    Args:
        path: Path to directory
        
    Returns:
        True if successful, False otherwise
    """
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return True
    except Exception as e:
        print(f"Failed to create directory {path}: {e}")
        return False


def safe_write(filepath: Union[str, Path], 
               content: Union[str, bytes],
               backup: bool = True) -> bool:
    """Write content to file safely with optional backup.
    
    Args:
        filepath: Path to file
        content: Content to write
        backup: Whether to create backup of existing file
        
    Returns:
        True if successful, False otherwise
    """
    filepath = Path(filepath)
    
    # Create backup if requested and file exists
    if backup and filepath.exists():
        backup_path = filepath.with_suffix(filepath.suffix + '.bak')
        try:
            backup_path.write_bytes(filepath.read_bytes())
        except Exception as e:
            print(f"Warning: Could not create backup: {e}")
    
    try:
        # Ensure parent directory exists
        ensure_directory(filepath.parent)
        
        # Write content
        if isinstance(content, str):
            filepath.write_text(content)
        else:
            filepath.write_bytes(content)
        return True
    except Exception as e:
        print(f"Failed to write to {filepath}: {e}")
        return False


def load_data(filepath: Union[str, Path], 
              format_hint: Optional[str] = None) -> Any:
    """Load data from file with automatic format detection.
    
    Args:
        filepath: Path to file
        format_hint: Optional format hint ('json', 'yaml', 'pickle')
        
    Returns:
        Loaded data object
    """
    filepath = Path(filepath)
    
    # Determine format
    if format_hint is None:
        ext = filepath.suffix.lower()
        if ext == '.json':
            format_hint = 'json'
        elif ext in ['.yml', '.yaml']:
            format_hint = 'yaml'
        elif ext == '.pkl':
            format_hint = 'pickle'
        else:
            raise ValueError(f"Could not determine format for {filepath}")
    
    try:
        with open(filepath, 'r') as f:
            if format_hint == 'json':
                return json.load(f)
            elif format_hint == 'yaml':
                return yaml.safe_load(f)
            elif format_hint == 'pickle':
                with open(filepath, 'rb') as pf:
                    return pickle.load(pf)
    except Exception as e:
        print(f"Failed to load data from {filepath}: {e}")
        raise


def save_data(data: Any, 
              filepath: Union[str, Path],
              format_hint: Optional[str] = None) -> bool:
    """Save data to file with automatic format detection.
    
    Args:
        data: Data to save
        filepath: Path to file
        format_hint: Optional format hint ('json', 'yaml', 'pickle')
        
    Returns:
        True if successful, False otherwise
    """
    filepath = Path(filepath)
    
    # Determine format
    if format_hint is None:
        ext = filepath.suffix.lower()
        if ext == '.json':
            format_hint = 'json'
        elif ext in ['.yml', '.yaml']:
            format_hint = 'yaml'
        elif ext == '.pkl':
            format_hint = 'pickle'
        else:
            format_hint = 'json'  # Default to JSON
    
    try:
        if format_hint == 'json':
            filepath.write_text(json.dumps(data, indent=2))
        elif format_hint == 'yaml':
            with open(filepath, 'w') as f:
                yaml.dump(data, f, default_flow_style=False)
        elif format_hint == 'pickle':
            with open(filepath, 'wb') as f:
                pickle.dump(data, f)
        return True
    except Exception as e:
        print(f"Failed to save data to {filepath}: {e}")
        return False
