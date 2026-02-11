#!/usr/bin/env python3
"""
Tests for I/O utilities.
"""

import pytest
import tempfile
import json
from pathlib import Path

from hephaestus.io.utils import (
    ensure_directory, 
    safe_write, 
    load_data, 
    save_data
)


def test_ensure_directory():
    """Test directory creation."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_path = Path(temp_dir) / "test" / "nested" / "directory"
        assert ensure_directory(test_path)
        assert test_path.exists()

def test_safe_write():
    """Test safe file writing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "test.txt"
        
        # Write content
        assert safe_write(test_file, "Hello World")
        assert test_file.exists()
        assert test_file.read_text() == "Hello World"
