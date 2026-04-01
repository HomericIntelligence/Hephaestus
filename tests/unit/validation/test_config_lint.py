#!/usr/bin/env python3
"""Unit tests for hephaestus.validation.config_lint."""

from pathlib import Path

from hephaestus.validation.config_lint import ConfigLinter


class TestStripInlineComment:
    """Tests for ConfigLinter._strip_inline_comment."""

    def test_plain_comment_stripped(self):
        """Hash preceded by space is stripped."""
        assert ConfigLinter._strip_inline_comment("key: value # comment") == "key: value "

    def test_hash_in_single_quotes_preserved(self):
        """Hash inside single-quoted value is NOT stripped."""
        assert (
            ConfigLinter._strip_inline_comment("key: 'val # not comment'")
            == "key: 'val # not comment'"
        )

    def test_hash_in_double_quotes_preserved(self):
        """Hash inside double-quoted value is NOT stripped."""
        assert (
            ConfigLinter._strip_inline_comment('key: "val # not comment"')
            == 'key: "val # not comment"'
        )

    def test_comment_after_double_quoted_value(self):
        """Comment after closing double-quote is stripped."""
        result = ConfigLinter._strip_inline_comment('key: "value" # comment')
        assert result == 'key: "value" '

    def test_backslash_escaped_quote_in_double_quoted(self):
        """Backslash-escaped quote does not close the double-quoted region."""
        line = r'key: "she said \"hi\" # not a comment" # real comment'
        result = ConfigLinter._strip_inline_comment(line)
        assert "# real comment" not in result
        assert '"hi"' in result or r"\"hi\"" in result

    def test_no_comment(self):
        """Lines without comment are returned unchanged."""
        assert ConfigLinter._strip_inline_comment("key: value") == "key: value"

    def test_empty_line(self):
        """Empty line is returned unchanged."""
        assert ConfigLinter._strip_inline_comment("") == ""

    def test_hash_without_preceding_space_not_stripped(self):
        """Hash not preceded by whitespace is not treated as comment delimiter."""
        assert (
            ConfigLinter._strip_inline_comment("key: value#not_comment") == "key: value#not_comment"
        )

    def test_trailing_backslash_at_end_of_double_quote(self):
        """Backslash at end of double-quoted string does not cause index error."""
        line = 'key: "value\\\\"'
        result = ConfigLinter._strip_inline_comment(line)
        assert result == line


class TestCountUnquoted:
    """Tests for ConfigLinter._count_unquoted."""

    def test_simple_balanced_braces(self):
        """Balanced braces return 0."""
        assert ConfigLinter._count_unquoted("{}", "{", "}") == 0

    def test_unmatched_open_brace(self):
        """Unmatched open brace returns positive."""
        assert ConfigLinter._count_unquoted("{key: val", "{", "}") == 1

    def test_brace_inside_double_quotes_ignored(self):
        """Brace inside double-quoted string is not counted."""
        assert ConfigLinter._count_unquoted('key: "{not a brace}"', "{", "}") == 0

    def test_brace_inside_single_quotes_ignored(self):
        """Brace inside single-quoted string is not counted."""
        assert ConfigLinter._count_unquoted("key: '{not a brace}'", "{", "}") == 0

    def test_unmatched_open_bracket(self):
        """Unmatched open bracket returns positive."""
        assert ConfigLinter._count_unquoted("[1, 2", "[", "]") == 1

    def test_bracket_inside_quotes_ignored(self):
        """Bracket inside quotes is not counted."""
        assert ConfigLinter._count_unquoted('key: "[not a bracket]"', "[", "]") == 0

    def test_backslash_escaped_quote_not_closing(self):
        r"""Escaped \" inside double-quoted value is not treated as close."""
        assert ConfigLinter._count_unquoted(r'key: "val \"not closed\" more"', "{", "}") == 0


class TestCheckYamlSyntax:
    """Tests for ConfigLinter._check_yaml_syntax (via lint_file)."""

    def _lint(self, content: str) -> ConfigLinter:
        """Lint *content* via _check_yaml_syntax and return the linter for assertions."""
        linter = ConfigLinter()
        linter._check_yaml_syntax(content, Path("test.yaml"))
        return linter

    def test_valid_yaml_passes(self):
        """Simple valid YAML passes syntax check."""
        linter = self._lint("key: value\nother: 123\n")
        assert not linter.errors

    def test_unmatched_braces_detected(self):
        """Unmatched opening brace produces an error."""
        linter = self._lint("key: {value\n")
        assert any("Unmatched braces" in e for e in linter.errors)

    def test_unmatched_brackets_detected(self):
        """Unmatched opening bracket produces an error."""
        linter = self._lint("key: [1, 2\n")
        assert any("Unmatched brackets" in e for e in linter.errors)

    def test_braces_in_string_not_false_positive(self):
        """Braces inside a quoted string do not trigger an error."""
        linter = self._lint('key: "{unclosed brace inside string"\n')
        assert not any("Unmatched braces" in e for e in linter.errors)

    def test_block_scalar_pipe(self):
        """Block scalar with | is detected; contents not scanned for braces."""
        linter = self._lint("key: |\n  {unclosed brace in block scalar\n")
        assert not any("Unmatched braces" in e for e in linter.errors)

    def test_block_scalar_greater_than(self):
        """Block scalar with > is detected."""
        linter = self._lint("key: >\n  [unclosed bracket in block scalar\n")
        assert not any("Unmatched brackets" in e for e in linter.errors)

    def test_block_scalar_with_chomp_strip(self):
        """Block scalar with |- (strip chomping) is recognized."""
        linter = self._lint("key: |-\n  {unclosed brace in block scalar\n")
        assert not any("Unmatched braces" in e for e in linter.errors)

    def test_block_scalar_with_chomp_keep(self):
        """Block scalar with |+ (keep chomping) is recognized."""
        linter = self._lint("key: |+\n  {unclosed brace in block scalar\n")
        assert not any("Unmatched braces" in e for e in linter.errors)

    def test_block_scalar_with_indent_indicator(self):
        """Block scalar with |2 (explicit indent) is recognized."""
        linter = self._lint("key: |2\n  {unclosed brace in block scalar\n")
        assert not any("Unmatched braces" in e for e in linter.errors)

    def test_block_scalar_folded_with_indicators(self):
        """Block scalar with >- (fold+strip) is recognized."""
        linter = self._lint("key: >-\n  [unclosed bracket in block scalar\n")
        assert not any("Unmatched brackets" in e for e in linter.errors)

    def test_balanced_inline_dict(self):
        """Balanced inline dict is valid."""
        linter = self._lint("key: {a: 1, b: 2}\n")
        assert not linter.errors

    def test_balanced_inline_list(self):
        """Balanced inline list is valid."""
        linter = self._lint("key: [1, 2, 3]\n")
        assert not linter.errors


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
