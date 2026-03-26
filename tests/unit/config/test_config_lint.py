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


class TestYamlSyntaxFalsePositives:
    """Tests that valid YAML constructs do not trigger false-positive malformed key warnings."""

    @pytest.mark.parametrize(
        "yaml_content, description",
        [
            ('description: "Time: 3:00pm"\n', "colon in quoted value"),
            ("created: 2024-01-15T10:30:00\n", "inline ISO timestamp"),
            ('"my key": value\n', "double-quoted key"),
            ("'my key': value\n", "single-quoted key"),
            ("mapping: {key: value}\n", "flow mapping in value"),
            ("{key: value, other: 2}\n", "top-level flow mapping"),
            ('items:\n  - "key: value"\n', "list item with colon in value"),
            ("items:\n  - name: foo\n", "list item as mapping"),
            ("url: https://example.com\n", "URL value"),
            ("key: value\n", "simple key-value pair"),
            ("---\nkey: value\n", "document separator"),
            ("key: value\n...\n", "document end marker"),
            (
                "desc: |\n  Line with: colon\n  Another: line\n",
                "literal block scalar",
            ),
            (
                "desc: >\n  Line with: colon\n  Another: line\n",
                "folded block scalar",
            ),
            ("# comment with: colon\nkey: value\n", "comment with colon"),
            ("message: 'Error: something failed'\n", "colon in single-quoted value"),
        ],
        ids=lambda d: d if isinstance(d, str) and ":" not in d else None,
    )
    def test_no_false_positive_warning(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
        yaml_content: str,
        description: str,
    ) -> None:
        """Valid YAML construct should not produce a malformed key warning."""
        path = yaml_file(yaml_content)
        linter.lint_file(path)
        malformed_warnings = [w for w in linter.warnings if "malformed key" in w.lower()]
        assert malformed_warnings == [], f"False positive for {description}: {malformed_warnings}"

    def test_actual_malformed_key_still_detected(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """A genuinely suspicious line should still produce a warning."""
        # A line like "  @weird:stuff" has a colon but doesn't match any valid pattern
        path = yaml_file("key: value\n@weird:stuff\n")
        linter.lint_file(path)
        malformed_warnings = [w for w in linter.warnings if "malformed key" in w.lower()]
        assert len(malformed_warnings) == 1

    def test_block_scalar_skips_inner_lines(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """Lines inside block scalars should not be checked for malformed keys."""
        content = (
            "description: |\n"
            "  not a key: this is block scalar text\n"
            "  another: line\n"
            "next_key: value\n"
        )
        path = yaml_file(content)
        linter.lint_file(path)
        malformed_warnings = [w for w in linter.warnings if "malformed key" in w.lower()]
        assert malformed_warnings == []


class TestBlockScalarBraceCounting:
    """Tests that braces/brackets inside block scalars do not trigger unmatched errors."""

    @pytest.mark.parametrize(
        "scalar_type, content",
        [
            ("|", "{"),
            ("|", "}"),
            ("|", "{key: value}"),
            ("|", "{{nested}}"),
            ("|", "{unclosed"),
            (">", "{"),
            (">", "}"),
            (">", "{key: value}"),
            (">", "{{nested}}"),
            (">", "{unclosed"),
        ],
        ids=[
            "literal-lone-open-brace",
            "literal-lone-close-brace",
            "literal-flow-mapping",
            "literal-double-braces",
            "literal-mismatched-brace",
            "folded-lone-open-brace",
            "folded-lone-close-brace",
            "folded-flow-mapping",
            "folded-double-braces",
            "folded-mismatched-brace",
        ],
    )
    def test_braces_inside_block_scalar_not_counted(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
        scalar_type: str,
        content: str,
    ) -> None:
        """Braces inside block scalars should not affect brace counting."""
        yaml_content = f"key: value\ndescription: {scalar_type}\n  {content}\nnext_key: value\n"
        path = yaml_file(yaml_content)
        result = linter.lint_file(path)
        brace_errors = [e for e in linter.errors if "Unmatched braces" in e]
        assert brace_errors == [], f"False brace error for {scalar_type} with '{content}'"
        assert result is True

    @pytest.mark.parametrize(
        "scalar_type, content",
        [
            ("|", "["),
            ("|", "]"),
            ("|", "[item1, item2]"),
            ("|", "[unclosed"),
            (">", "["),
            (">", "]"),
            (">", "[item1, item2]"),
            (">", "[unclosed"),
        ],
        ids=[
            "literal-lone-open-bracket",
            "literal-lone-close-bracket",
            "literal-flow-sequence",
            "literal-mismatched-bracket",
            "folded-lone-open-bracket",
            "folded-lone-close-bracket",
            "folded-flow-sequence",
            "folded-mismatched-bracket",
        ],
    )
    def test_brackets_inside_block_scalar_not_counted(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
        scalar_type: str,
        content: str,
    ) -> None:
        """Brackets inside block scalars should not affect bracket counting."""
        yaml_content = f"key: value\nitems: {scalar_type}\n  {content}\nnext_key: value\n"
        path = yaml_file(yaml_content)
        result = linter.lint_file(path)
        bracket_errors = [e for e in linter.errors if "Unmatched brackets" in e]
        assert bracket_errors == [], f"False bracket error for {scalar_type} with '{content}'"
        assert result is True

    def test_mixed_braces_and_brackets_inside_block_scalar(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """Mixed braces and brackets inside a block scalar should not trigger errors."""
        content = (
            "key: value\n"
            "description: |\n"
            "  {some text\n"
            "  [more text\n"
            "  {mixed] content\n"
            "  {{nested[bracket\n"
            "next_key: value\n"
        )
        path = yaml_file(content)
        result = linter.lint_file(path)
        brace_errors = [e for e in linter.errors if "Unmatched braces" in e]
        bracket_errors = [e for e in linter.errors if "Unmatched brackets" in e]
        assert brace_errors == []
        assert bracket_errors == []
        assert result is True

    def test_braces_outside_block_scalar_still_detected(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """Unmatched braces outside block scalars should still trigger errors."""
        path = yaml_file("key: { unclosed\n")
        result = linter.lint_file(path)
        assert result is False
        assert any("Unmatched braces" in e for e in linter.errors)

    def test_brackets_outside_block_scalar_still_detected(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """Unmatched brackets outside block scalars should still trigger errors."""
        path = yaml_file("key: [ unclosed\n")
        result = linter.lint_file(path)
        assert result is False
        assert any("Unmatched brackets" in e for e in linter.errors)

    def test_braces_after_block_scalar_ends_still_counted(
        self,
        linter: ConfigLinter,
        yaml_file: Any,
    ) -> None:
        """Brace counting should resume after a block scalar ends."""
        content = "description: |\n  {safe inside block scalar\nbroken: { unclosed\n"
        path = yaml_file(content)
        result = linter.lint_file(path)
        assert result is False
        assert any("Unmatched braces" in e for e in linter.errors)


class TestStripInlineComment:
    """Tests for ConfigLinter._strip_inline_comment."""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ('color: "#FF0000"', 'color: "#FF0000"'),
            ("color: '#FF0000'", "color: '#FF0000'"),
            ('desc: "text with # in middle"', 'desc: "text with # in middle"'),
            ("value: plain text # comment", "value: plain text "),
            (
                'value: "quoted # not comment" # real',
                'value: "quoted # not comment" ',
            ),
            ("# full line comment", ""),
            ("value: plain text", "value: plain text"),
            ("value: no#space", "value: no#space"),
            ("", ""),
            ("  # indented comment", "  "),
            ("key: 'single # hash' # comment", "key: 'single # hash' "),
        ],
        ids=[
            "hex-color-double-quotes",
            "hex-color-single-quotes",
            "hash-mid-double-string",
            "legitimate-inline-comment",
            "quoted-hash-and-real-comment",
            "full-line-comment",
            "no-hash",
            "hash-without-preceding-space",
            "empty-line",
            "indented-comment",
            "single-quoted-hash-and-comment",
        ],
    )
    def test_strip_inline_comment(self, line: str, expected: str) -> None:
        """_strip_inline_comment handles various quote/comment scenarios."""
        assert ConfigLinter._strip_inline_comment(line) == expected


class TestLintFileQuotedHash:
    """Integration tests for hex colors and quoted # in YAML files."""

    def test_hex_color_no_false_brace_error(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """YAML with hex color in quotes should not produce brace mismatch errors."""
        path = yaml_file('color: "#FF0000"\n')
        result = linter.lint_file(path)
        assert result is True
        assert not any("brace" in e.lower() for e in linter.errors)

    def test_quoted_hash_preserves_content(self, linter: ConfigLinter, yaml_file: Any) -> None:
        """YAML with # inside quotes passes linting without syntax errors."""
        path = yaml_file('title: "Section # 1"\ndesc: "Item #2"\n')
        result = linter.lint_file(path)
        assert result is True
        assert linter.errors == []


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
