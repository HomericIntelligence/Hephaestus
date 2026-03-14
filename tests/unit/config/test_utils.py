#!/usr/bin/env python3
"""Tests for configuration utilities.
"""


from hephaestus.config.utils import get_setting, validate_config


def test_get_setting():
    """Test getting settings with dot notation."""
    config = {
        "database": {
            "host": "localhost",
            "port": 5432
        },
        "api": {
            "timeout": 30
        }
    }

    # Test existing nested key
    assert get_setting(config, "database.host") == "localhost"
    assert get_setting(config, "database.port") == 5432
    assert get_setting(config, "api.timeout") == 30

    # Test non-existing key with default
    assert get_setting(config, "database.user", "admin") == "admin"
    assert get_setting(config, "non.existing.key") is None

def test_validate_config():
    """Test configuration validation."""
    config = {"test": "value"}
    schema = {"test": str}

    # Currently just a placeholder test
    assert validate_config(config, schema) is True
