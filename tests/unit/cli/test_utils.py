#!/usr/bin/env python3
"""Tests for CLI utilities."""

import argparse
import json

import pytest

from hephaestus.cli.utils import (
    CommandRegistry,
    add_logging_args,
    create_parser,
    format_output,
    format_table,
)


class TestCommandRegistry:
    """Tests for CommandRegistry."""

    def test_register_and_retrieve(self) -> None:
        """Can register a command and retrieve it by name."""
        registry = CommandRegistry()

        @registry.register("my-cmd", description="a test command")
        def my_cmd() -> None:
            pass

        result = registry.get_command("my-cmd")
        assert result is not None
        assert result["function"] is my_cmd
        assert result["description"] == "a test command"

    def test_aliases(self) -> None:
        """Registered aliases also resolve to the command."""
        registry = CommandRegistry()

        @registry.register("cmd", aliases=["c", "co"])
        def cmd() -> None:
            pass

        assert registry.get_command("c") is not None
        assert registry.get_command("co") is not None

    def test_missing_command_returns_none(self) -> None:
        """get_command returns None for unregistered names."""
        registry = CommandRegistry()
        assert registry.get_command("nope") is None


class TestCreateParser:
    """Tests for create_parser."""

    def test_returns_argument_parser(self) -> None:
        """create_parser returns an ArgumentParser."""
        parser = create_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_version_flag_exists(self) -> None:
        """Parser has --version / -V action."""
        parser = create_parser()
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0

    def test_custom_prog_name(self) -> None:
        """Prog name is set correctly."""
        parser = create_parser("myprog")
        assert parser.prog == "myprog"


class TestAddLoggingArgs:
    """Tests for add_logging_args."""

    def test_adds_verbose_flag(self) -> None:
        """--verbose flag is added."""
        parser = argparse.ArgumentParser()
        add_logging_args(parser)
        args = parser.parse_args(["--verbose"])
        assert args.verbose is True

    def test_adds_quiet_flag(self) -> None:
        """--quiet flag is added."""
        parser = argparse.ArgumentParser()
        add_logging_args(parser)
        args = parser.parse_args(["--quiet"])
        assert args.quiet is True

    def test_adds_log_file(self) -> None:
        """--log-file argument is added."""
        parser = argparse.ArgumentParser()
        add_logging_args(parser)
        args = parser.parse_args(["--log-file", "out.log"])
        assert args.log_file == "out.log"


class TestFormatTable:
    """Tests for format_table."""

    def test_basic_table(self) -> None:
        """Basic table renders rows correctly."""
        rows = [["alice", "30"], ["bob", "25"]]
        output = format_table(rows)
        assert "alice" in output
        assert "bob" in output

    def test_with_headers(self) -> None:
        """Headers appear in output with separator line."""
        rows = [["alice", "30"]]
        output = format_table(rows, headers=["Name", "Age"])
        assert "Name" in output
        assert "Age" in output
        assert "---" in output

    def test_empty_rows(self) -> None:
        """Empty rows returns empty string."""
        assert format_table([]) == ""

    def test_empty_rows_with_empty_headers(self) -> None:
        """Empty rows with headers returns empty string (no headers, no rows)."""
        assert format_table([], headers=[]) == ""


class TestFormatOutput:
    """Tests for format_output."""

    def test_json_format(self) -> None:
        """JSON format produces valid JSON."""
        data = {"key": "value", "num": 42}
        result = format_output(data, format_type="json")
        parsed = json.loads(result)
        assert parsed == data

    def test_text_dict(self) -> None:
        """Text format of dict contains key: value lines."""
        data = {"name": "hephaestus", "version": "0.3.0"}
        result = format_output(data, format_type="text")
        assert "name: hephaestus" in result

    def test_text_list(self) -> None:
        """Text format of list joins items with newlines."""
        data = ["a", "b", "c"]
        result = format_output(data, format_type="text")
        assert result == "a\nb\nc"

    def test_table_dict_rows(self) -> None:
        """Table format of list-of-dicts renders headers and rows."""
        data = [{"name": "alice", "age": "30"}, {"name": "bob", "age": "25"}]
        result = format_output(data, format_type="table")
        assert "name" in result
        assert "alice" in result

    def test_table_list_rows(self) -> None:
        """Table format of list-of-lists renders rows."""
        data = [["alice", "30"], ["bob", "25"]]
        result = format_output(data, format_type="table")
        assert "alice" in result

    def test_scalar_text(self) -> None:
        """Text format of a scalar returns its string representation."""
        assert format_output(42) == "42"
