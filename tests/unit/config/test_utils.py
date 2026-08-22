#!/usr/bin/env python3
"""Tests for configuration utilities."""

import sys

import pytest
import yaml

from hephaestus.config.utils import (
    get_setting,
    load_config,
    load_yaml_config,
    merge_configs,
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

    def test_load_yaml_without_pyyaml_raises_runtime_error(self, tmp_path, monkeypatch):
        """Missing PyYAML on a .yaml file raises RuntimeError, not ValueError (issue #1510)."""
        monkeypatch.setitem(sys.modules, "yaml", None)
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\n")
        with pytest.raises(RuntimeError, match=r"Install with: pip install PyYAML"):
            load_config(yaml_file)

    def test_load_yml_without_pyyaml_raises_runtime_error(self, tmp_path, monkeypatch):
        """The .yml extension also reports the missing dependency, not a format error."""
        monkeypatch.setitem(sys.modules, "yaml", None)
        yml_file = tmp_path / "config.yml"
        yml_file.write_text("key: value\n")
        with pytest.raises(RuntimeError, match=r"Install with: pip install PyYAML"):
            load_config(yml_file)

    def test_load_yaml_without_pyyaml_is_not_value_error(self, tmp_path, monkeypatch):
        """Regression (issue #1510): missing-PyYAML must NOT raise 'Unsupported config format'."""
        monkeypatch.setitem(sys.modules, "yaml", None)
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\n")
        with pytest.raises(RuntimeError, match=r"Install with: pip install PyYAML"):
            load_config(yaml_file)


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

    def test_empty_key_path_returns_default(self, sample_config):
        """Empty key_path returns default without raising."""
        assert get_setting(sample_config, "", "fallback") == "fallback"

    def test_leading_dot_returns_default(self, sample_config):
        """Leading dot is treated as empty leading segment; still resolves valid remainder."""
        # ".database.host" → segments ["database", "host"] → valid lookup
        assert get_setting(sample_config, ".database.host") == "localhost"

    def test_trailing_dot_returns_default(self, sample_config):
        """Trailing dot is treated as empty trailing segment; still resolves valid prefix."""
        # "database.host." → segments ["database", "host"] → valid lookup
        assert get_setting(sample_config, "database.host.") == "localhost"

    def test_double_dot_resolves_valid_segments(self, sample_config):
        """Double dot produces empty segment which is filtered; valid segments still resolve."""
        # "database..host" → segments ["database", "host"] → valid lookup
        assert get_setting(sample_config, "database..host") == "localhost"


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

    def test_empty_string_key_skipped(self, caplog):
        """Empty-string keys in override dict are skipped with a warning."""
        import logging

        config_logger = logging.getLogger("hephaestus.config.utils")
        original_propagate = config_logger.propagate
        config_logger.propagate = True
        try:
            base: dict = {"a": 1}
            override: dict = {"": "bad", "b": 2}
            with caplog.at_level(logging.WARNING, logger="hephaestus.config.utils"):
                result = merge_configs(base, override)
        finally:
            config_logger.propagate = original_propagate
        assert "" not in result
        assert result.get("b") == 2
        assert any("empty or non-string key" in m for m in caplog.messages)

    def test_non_string_key_skipped(self, caplog):
        """Non-string keys in override dict are skipped with a warning."""
        import logging

        config_logger = logging.getLogger("hephaestus.config.utils")
        original_propagate = config_logger.propagate
        config_logger.propagate = True
        try:
            base: dict = {"a": 1}
            override: dict = {42: "bad", "b": 2}
            with caplog.at_level(logging.WARNING, logger="hephaestus.config.utils"):
                result = merge_configs(base, override)
        finally:
            config_logger.propagate = original_propagate
        assert 42 not in result
        assert result.get("b") == 2
        assert any("empty or non-string key" in m for m in caplog.messages)


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

    def test_load_yaml_config_without_pyyaml_is_actionable(self, tmp_path, monkeypatch):
        """The YAML-specific entry point uses the shared missing-dependency error."""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("key: value\n")
        monkeypatch.setitem(sys.modules, "yaml", None)

        with pytest.raises(RuntimeError, match=r"Install with: pip install PyYAML"):
            load_yaml_config(yaml_file)
