#!/usr/bin/env python3
"""Tests for configuration linting utilities."""

from pathlib import Path
from typing import Any

import pytest

from hephaestus.validation.config_lint import ConfigLinter

ML_DEPRECATED_KEYS = {
    "optimizer.type": "optimizer.name",
    "model.num_layers": "model.layers",
    "lr": "learning_rate",
    "val_split": "validation_split",
}

ML_REQUIRED_KEYS = {
    "training": ["epochs", "batch_size"],
    "model": ["architecture"],
    "optimizer": ["name", "learning_rate"],
}

ML_PERF_THRESHOLDS: dict[str, tuple[float, float]] = {
    "batch_size": (8, 512),
    "learning_rate": (0.00001, 1.0),
    "epochs": (1, 10000),
}


@pytest.fixture()
def linter() -> ConfigLinter:
    """Return a ConfigLinter instance with ML-domain defaults."""
    return ConfigLinter(
        deprecated_keys=ML_DEPRECATED_KEYS,
        required_keys=ML_REQUIRED_KEYS,
        perf_thresholds=ML_PERF_THRESHOLDS,
    )


@pytest.fixture()
def yaml_file(tmp_path: Path):
    """Write a temp YAML file and return its path."""

    def _write(content: str) -> Path:
        p = tmp_path / "config.yaml"
        p.write_text(content)
        return p

    return _write


class TestConfigLinterInit:
    """Tests for ConfigLinter initialization."""

    def test_defaults_empty(self) -> None:
        """Default ConfigLinter has empty deprecated/required/perf dicts."""
        default_linter = ConfigLinter()
        assert default_linter.deprecated_keys == {}
        assert default_linter.required_keys == {}
        assert default_linter.perf_thresholds == {}

    def test_ml_fixture_has_keys(self, linter: ConfigLinter) -> None:
        """Fixture linter has ML-domain keys populated."""
        assert "lr" in linter.deprecated_keys
        assert "training" in linter.required_keys
        assert "batch_size" in linter.perf_thresholds

    def test_custom_deprecated_keys(self) -> None:
        """Custom deprecated_keys are stored as-is."""
        custom = {"old_key": "new_key"}
        linter_obj = ConfigLinter(deprecated_keys=custom)
        assert linter_obj.deprecated_keys == custom


class TestLintFile:
    """Tests for ConfigLinter.lint_file."""

    def test_valid_yaml(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Valid YAML file with no issues passes linting."""
        path = yaml_file("key: value\n")
        assert linter.lint_file(path) is True
        assert linter.errors == []

    def test_missing_file(self, linter: ConfigLinter, tmp_path: Path) -> None:
        """Linting a non-existent file returns False."""
        result = linter.lint_file(tmp_path / "ghost.yaml")
        assert result is False
        assert any("not found" in e.lower() for e in linter.errors)

    def test_unmatched_brace(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Unmatched brace detected as error."""
        path = yaml_file("key: { unclosed\n")
        result = linter.lint_file(path)
        assert result is False

    def test_tabs_generate_warning(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Tabs in YAML generate a warning."""
        path = yaml_file("key:\n\tvalue: 1\n")
        linter.lint_file(path)
        assert any("tab" in w.lower() for w in linter.warnings)

    def test_trailing_whitespace_suggestion(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Trailing whitespace generates a suggestion."""
        path = yaml_file("key: value   \n")
        linter.lint_file(path)
        assert any("trailing" in s.lower() for s in linter.suggestions)

    def test_deprecated_key_warning(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Deprecated key generates a warning."""
        path = yaml_file("lr: 0.001\n")
        linter.lint_file(path)
        assert any("lr" in w for w in linter.warnings)

    def test_perf_threshold_warning(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """Out-of-range performance parameter generates a warning."""
        path = yaml_file("batch_size: 2\n")
        linter.lint_file(path)
        assert any("batch_size" in w for w in linter.warnings)


class TestPrintResults:
    """Tests for ConfigLinter.print_results."""

    def test_no_issues(self, linter: ConfigLinter, capsys: Any) -> None:
        """print_results with no issues logs success."""
        linter.print_results()
        # Should not raise; the actual log output goes through logging, not capsys

    def test_with_errors(self, linter: ConfigLinter) -> None:
        """print_results with errors does not raise."""
        linter.errors = ["some error"]
        linter.warnings = ["some warning"]
        linter.suggestions = ["some suggestion"]
        linter.print_results()  # should not raise
