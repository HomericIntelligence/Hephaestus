#!/usr/bin/env python3
"""Unit tests for hephaestus.validation.config_lint."""

from pathlib import Path

from hephaestus.validation.config_lint import ConfigLinter


class TestLintFileSyntaxErrors:
    """Syntax-error detection now flows through yaml.safe_load."""

    def test_unmatched_braces_detected(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text("key: {value\n")
        linter = ConfigLinter()
        assert linter.lint_file(f) is False
        assert any("YAML syntax error" in e for e in linter.errors)

    def test_unmatched_brackets_detected(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text("key: [1, 2\n")
        linter = ConfigLinter()
        assert linter.lint_file(f) is False
        assert any("YAML syntax error" in e for e in linter.errors)

    def test_braces_in_string_not_false_positive(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text('key: "{unclosed brace inside string"\n')
        linter = ConfigLinter()
        assert linter.lint_file(f) is True

    def test_block_scalar_pipe_allows_braces(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text("key: |\n  {unclosed brace in block scalar\n")
        linter = ConfigLinter()
        assert linter.lint_file(f) is True

    def test_block_scalar_folded_allows_brackets(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text("key: >\n  [unclosed bracket in block scalar\n")
        linter = ConfigLinter()
        assert linter.lint_file(f) is True

    def test_error_includes_line_number(self, tmp_path: Path):
        f = tmp_path / "c.yaml"
        f.write_text("good: line\nbad: : : :\n")
        linter = ConfigLinter()
        linter.lint_file(f)
        assert any(":2" in e for e in linter.errors)


class TestLintFile:
    """Integration tests for ConfigLinter.lint_file."""

    def test_lint_valid_file(self, tmp_path: Path):
        """Valid YAML file passes linting."""
        f = tmp_path / "config.yaml"
        f.write_text("key: value\nnested:\n  sub: 123\n")
        linter = ConfigLinter()
        result = linter.lint_file(f)
        assert result is True
        assert not linter.errors

    def test_lint_missing_file(self, tmp_path: Path):
        """Missing file returns False with an error."""
        linter = ConfigLinter()
        result = linter.lint_file(tmp_path / "missing.yaml")
        assert result is False
        assert linter.errors

    def test_lint_non_yaml_extension(self, tmp_path: Path):
        """Non-YAML file is skipped (returns True with no errors)."""
        f = tmp_path / "config.txt"
        f.write_text("not yaml")
        linter = ConfigLinter()
        result = linter.lint_file(f)
        assert result is True

    def test_deprecated_key_warning(self, tmp_path: Path):
        """Deprecated key produces a warning."""
        f = tmp_path / "config.yaml"
        f.write_text("old_key: value\n")
        linter = ConfigLinter(deprecated_keys={"old_key": "new_key"})
        linter.lint_file(f)
        assert any("old_key" in w for w in linter.warnings)
