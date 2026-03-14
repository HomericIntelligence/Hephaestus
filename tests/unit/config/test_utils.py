#!/usr/bin/env python3
"""Tests for configuration utilities."""

import pytest
import yaml

from hephaestus.config.utils import (
    get_config_value,
    get_setting,
    load_config,
    load_yaml_config,
    merge_configs,
    merge_with_env,
    validate_config,
)


class TestLoadConfig:
    """Tests for load_config."""

    def test_load_yaml_config(self, tmp_config_yaml):
        """Load a YAML config file successfully."""
        config = load_config(tmp_config_yaml)
        assert config["database"]["host"] == "localhost"
        assert config["database"]["port"] == 5432

    def test_load_json_config(self, tmp_config_json):
        """Load a JSON config file successfully."""
        config = load_config(tmp_config_json)
        assert config["app"]["name"] == "test"
        assert config["logging"]["level"] == "INFO"

    def test_load_nonexistent_raises(self, tmp_path):
        """Load raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.yaml")

    def test_load_unsupported_format_raises(self, tmp_path):
        """Load raises ValueError for unsupported extension."""
        bad_file = tmp_path / "config.toml"
        bad_file.write_text("[section]\nkey = 'value'\n")
        with pytest.raises(ValueError, match="Unsupported config format"):
            load_config(bad_file)

    def test_load_empty_yaml_returns_empty_dict(self, tmp_path):
        """Empty YAML file returns empty dict, not None."""
        empty_yaml = tmp_path / "empty.yaml"
        empty_yaml.write_text("")
        config = load_config(empty_yaml)
        assert config == {}

    def test_load_config_accepts_string_path(self, tmp_config_yaml):
        """load_config accepts a string path, not just Path objects."""
        config = load_config(str(tmp_config_yaml))
        assert isinstance(config, dict)

    def test_load_yml_extension(self, tmp_path):
        """load_config handles .yml extension (not just .yaml)."""
        data = {"key": "value"}
        config_file = tmp_path / "config.yml"
        config_file.write_text(yaml.dump(data))
        config = load_config(config_file)
        assert config["key"] == "value"


class TestGetSetting:
    """Tests for get_setting."""

    def test_simple_key(self, sample_config):
        """Get a top-level key."""
        assert get_setting(sample_config, "feature_flags") == {"new_ui": True, "beta_api": False}

    def test_nested_key(self, sample_config):
        """Get a nested key with dot notation."""
        assert get_setting(sample_config, "database.host") == "localhost"
        assert get_setting(sample_config, "database.port") == 5432

    def test_deeply_nested_key(self, sample_config):
        """Get a deeply nested key."""
        assert get_setting(sample_config, "database.credentials.user") == "admin"

    def test_missing_key_returns_none(self, sample_config):
        """Missing key returns None by default."""
        assert get_setting(sample_config, "does.not.exist") is None

    def test_missing_key_with_default(self, sample_config):
        """Missing key returns provided default."""
        assert get_setting(sample_config, "missing.key", "fallback") == "fallback"

    def test_missing_key_default_zero(self, sample_config):
        """Default value of 0 is returned (not confused with None)."""
        assert get_setting(sample_config, "missing", 0) == 0

    def test_intermediate_key_not_dict(self, sample_config):
        """Returns default when intermediate key is not a dict."""
        assert get_setting(sample_config, "database.port.sub") is None

    def test_empty_config(self):
        """Works on empty config dict."""
        assert get_setting({}, "any.key", "default") == "default"


class TestValidateConfig:
    """Tests for validate_config."""

    def test_valid_config(self):
        """Valid config against matching schema returns True."""
        config = {"name": "test", "value": 42}
        schema = {"name": str, "value": int}
        assert validate_config(config, schema) is True

    def test_missing_key_returns_false(self):
        """Config missing required key returns False."""
        config = {"name": "test"}
        schema = {"name": str, "required_field": str}
        assert validate_config(config, schema) is False

    def test_wrong_type_returns_false(self):
        """Config with wrong type returns False."""
        config = {"name": 42}
        schema = {"name": str}
        assert validate_config(config, schema) is False

    def test_empty_schema_always_valid(self):
        """Empty schema validates any config."""
        assert validate_config({"anything": True}, {}) is True

    def test_none_type_in_schema(self):
        """Schema with None type skips type check."""
        config = {"key": "value"}
        schema = {"key": None}
        assert validate_config(config, schema) is True


class TestMergeConfigs:
    """Tests for merge_configs."""

    def test_merge_two_dicts(self):
        """Merge two configs: later overrides earlier."""
        base = {"a": 1, "b": 2}
        override = {"b": 99, "c": 3}
        result = merge_configs(base, override)
        assert result == {"a": 1, "b": 99, "c": 3}

    def test_deep_merge(self):
        """Deep merge preserves nested keys not in override."""
        base = {"db": {"host": "localhost", "port": 5432}}
        override = {"db": {"port": 5433}}
        result = merge_configs(base, override)
        assert result["db"]["host"] == "localhost"
        assert result["db"]["port"] == 5433

    def test_merge_three_configs(self):
        """Merging three configs applies in order."""
        c1 = {"a": 1}
        c2 = {"a": 2, "b": 2}
        c3 = {"a": 3, "c": 3}
        result = merge_configs(c1, c2, c3)
        assert result == {"a": 3, "b": 2, "c": 3}

    def test_none_config_skipped(self):
        """None configs are skipped gracefully."""
        result = merge_configs({"a": 1}, None, {"b": 2})
        assert result == {"a": 1, "b": 2}

    def test_empty_merge(self):
        """No args returns empty dict."""
        assert merge_configs() == {}


class TestMergeWithEnv:
    """Tests for merge_with_env."""

    def test_env_variable_overrides(self, monkeypatch):
        """Environment variable overrides config value."""
        monkeypatch.setenv("HEPHAESTUS_DATABASE_HOST", "envhost")
        config = {"database": {"host": "localhost"}}
        result = merge_with_env(config)
        assert result["database"]["host"] == "envhost"

    def test_env_int_conversion(self, monkeypatch):
        """Numeric env vars are converted to int."""
        monkeypatch.setenv("HEPHAESTUS_PORT", "8080")
        result = merge_with_env({})
        assert result["port"] == 8080

    def test_env_float_conversion(self, monkeypatch):
        """Float env vars are converted to float."""
        monkeypatch.setenv("HEPHAESTUS_THRESHOLD", "0.75")
        result = merge_with_env({})
        assert result["threshold"] == 0.75

    def test_custom_prefix(self, monkeypatch):
        """Custom prefix is respected."""
        monkeypatch.setenv("MYAPP_KEY", "val")
        result = merge_with_env({}, prefix="MYAPP_")
        assert result["key"] == "val"

    def test_non_matching_env_ignored(self, monkeypatch):
        """Env vars without the prefix are ignored."""
        monkeypatch.setenv("OTHER_VAR", "ignored")
        config = {"a": 1}
        result = merge_with_env(config)
        assert result == {"a": 1}


class TestLoadYamlConfig:
    """Tests for load_yaml_config."""

    def test_load_yaml_config_success(self, tmp_config_yaml):
        """load_yaml_config loads a YAML file."""
        config = load_yaml_config(tmp_config_yaml)
        assert "database" in config

    def test_load_yaml_config_missing_file_raises(self, tmp_path):
        """load_yaml_config raises FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError):
            load_yaml_config(tmp_path / "missing.yaml")


class TestGetConfigValue:
    """Tests for get_config_value."""

    def test_returns_default_when_not_found(self):
        """Returns default when config key is not found."""
        result = get_config_value("nonexistent.key", default="fallback")
        assert result == "fallback"

    def test_returns_none_when_no_default(self):
        """Returns None when key not found and no default given."""
        result = get_config_value("nonexistent.key")
        assert result is None

    def test_with_config_files(self, tmp_config_yaml):
        """Loads from provided config_files list."""
        result = get_config_value("database.host", config_files=[str(tmp_config_yaml)])
        assert result == "localhost"
