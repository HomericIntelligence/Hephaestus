#!/usr/bin/env python3
"""
Tests for general utilities.
"""

import pytest
from hephaestus.utils.helpers import (
    slugify,
    human_readable_size,
    flatten_dict
)


def test_slugify():
    """Test text slugification."""
    assert slugify("Hello World") == "hello-world"
    assert slugify("My Project v1.0") == "my-project-v1-0"
    assert slugify("Special!@#$%Characters") == "specialcharacters"

def test_human_readable_size():
    """Test size conversion."""
    assert human_readable_size(0) == "0 B"
    assert human_readable_size(1023) == "1023.0 B"
    assert human_readable_size(1024) == "1.0 KB"
    assert human_readable_size(1048576) == "1.0 MB"

def test_flatten_dict():
    """Test dictionary flattening."""
    nested = {
        "a": 1,
        "b": {
            "c": 2,
            "d": {
                "e": 3
            }
        }
    }
    
    expected = {
        "a": 1,
        "b.c": 2,
        "b.d.e": 3
    }
    
    assert flatten_dict(nested) == expected
