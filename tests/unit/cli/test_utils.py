#!/usr/bin/env python3
"""Tests for CLI utilities."""

import argparse
import json
from unittest.mock import patch

import pytest

from hephaestus.cli.utils import (
    CommandRegistry,
    add_github_throttle_args,
    add_json_arg,
    add_logging_args,
    add_version_arg,
    configure_github_throttle_from_args,
    confirm_action,
    create_parser,
    emit_json_status,
    format_output,
    format_table,
)


class TestConfirmAction:
    """Tests for confirm_action."""

    def test_yes_response(self) -> None:
        """Returns True when user enters 'y'."""
        with patch("builtins.input", return_value="y"):
            assert confirm_action() is True

    def test_no_response(self) -> None:
        """Returns False when user enters 'n'."""
        with patch("builtins.input", return_value="n"):
            assert confirm_action() is False

    def test_default_on_empty_input(self) -> None:
        """Returns default when user just presses Enter."""
        with patch("builtins.input", return_value=""):
            assert confirm_action(default=True) is True
            assert confirm_action(default=False) is False

    def test_invalid_then_valid(self) -> None:
        """Invalid input retries; accepts valid answer on second attempt."""
        with patch("builtins.input", side_effect=["bad", "y"]):
            assert confirm_action() is True

    def test_max_attempts_returns_default(self) -> None:
        """After max_attempts of invalid input, returns default."""
        with patch("builtins.input", return_value="bad"):
            assert confirm_action(default=True, max_attempts=2) is True

    def test_yes_long_form(self) -> None:
        """'yes' is accepted as affirmative."""
        with patch("builtins.input", return_value="yes"):
            assert confirm_action() is True

    def test_no_long_form(self) -> None:
        """'no' is accepted as negative."""
        with patch("builtins.input", return_value="no"):
            assert confirm_action() is False


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


class TestAddVersionArg:
    """Tests for add_version_arg."""

    def test_adds_long_form(self) -> None:
        """add_version_arg() registers --version as a version action."""
        parser = argparse.ArgumentParser(prog="demo")
        add_version_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["--version"])
        assert exc.value.code == 0

    def test_adds_short_form(self) -> None:
        """-V is the short form for --version."""
        parser = argparse.ArgumentParser(prog="demo")
        add_version_arg(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args(["-V"])
        assert exc.value.code == 0

    def test_version_string_includes_prog(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Version output includes the prog name."""
        parser = argparse.ArgumentParser(prog="demo")
        add_version_arg(parser)
        with pytest.raises(SystemExit):
            parser.parse_args(["--version"])
        captured = capsys.readouterr()
        assert "demo" in captured.out


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


class TestAddGithubThrottleArgs:
    """Tests for shared GitHub throttle CLI options."""

    def test_defaults(self) -> None:
        parser = argparse.ArgumentParser()
        add_github_throttle_args(parser)
        args = parser.parse_args([])
        assert args.gh_global_rate == 10.0
        assert args.gh_global_burst == 30.0

    def test_custom_values(self) -> None:
        parser = argparse.ArgumentParser()
        add_github_throttle_args(parser)
        args = parser.parse_args(["--gh-global-rate", "5.5", "--gh-global-burst", "12"])
        assert args.gh_global_rate == 5.5
        assert args.gh_global_burst == 12.0

    def test_zero_rate_allowed(self) -> None:
        parser = argparse.ArgumentParser()
        add_github_throttle_args(parser)
        args = parser.parse_args(["--gh-global-rate", "0"])
        assert args.gh_global_rate == 0.0

    @pytest.mark.parametrize(
        ("flag", "value"),
        [
            ("--gh-global-rate", "-1"),
            ("--gh-global-rate", "nan"),
            ("--gh-global-burst", "0"),
            ("--gh-global-burst", "0.5"),
            ("--gh-global-burst", "-1"),
        ],
    )
    def test_invalid_values_exit_2(self, flag: str, value: str) -> None:
        parser = argparse.ArgumentParser()
        add_github_throttle_args(parser)
        with pytest.raises(SystemExit) as exc:
            parser.parse_args([flag, value])
        assert exc.value.code == 2

    def test_configure_from_args(self) -> None:
        parser = argparse.ArgumentParser()
        add_github_throttle_args(parser)
        args = parser.parse_args(["--gh-global-rate", "4", "--gh-global-burst", "9"])
        with patch("hephaestus.github.rate_limit.configure_gh_global_throttle") as mock_configure:
            configure_github_throttle_from_args(args)
        mock_configure.assert_called_once_with(rate=4.0, burst=9.0)


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

    def test_ragged_rows_pad_to_max_columns(self) -> None:
        """Short rows are padded so every line has the same rendered width."""
        rows = [["a"], ["bb", "cc"], ["d", "e"]]
        output = format_table(rows)
        lines = output.split("\n")
        assert len({len(line) for line in lines}) == 1, lines

    def test_ragged_rows_column_widths_use_widest_cell(self) -> None:
        """Column widths come from the widest cell in each column across all rows."""
        rows = [["a"], ["bbbb", "cc"]]
        output = format_table(rows)
        lines = output.split("\n")
        # col_widths=[4,2], separator="  " → row 0: "a   " + "  " + "  " = "a       "
        # row 1: "bbbb" + "  " + "cc" = "bbbb  cc". Both 8 chars; col 1 of row 0 is "  ".
        assert lines[0] == "a   " + "  " + "  "
        assert lines[1] == "bbbb" + "  " + "cc"

    def test_headers_shorter_than_rows(self) -> None:
        """Headers with fewer columns than rows still produce equal-width lines."""
        rows = [["alice", "30", "eng"]]
        output = format_table(rows, headers=["Name"])
        lines = output.split("\n")
        # Header line, separator line, one data row.
        assert len(lines) == 3
        assert len({len(line) for line in lines}) == 1, lines

    def test_separator_dash_spans_full_data_width(self) -> None:
        """Dash separator widens to data column count when headers are shorter."""
        rows = [["alice", "30", "eng"]]
        output = format_table(rows, headers=["Name"])
        lines = output.split("\n")
        # Dash row must contain three dash-runs joined by the separator, not one.
        # Count dash-runs by splitting on the column separator "  ".
        dash_groups = [g for g in lines[1].split("  ") if set(g) == {"-"}]
        assert len(dash_groups) == 3, lines[1]

    def test_rows_with_only_empty_inner_list(self) -> None:
        """A single empty-list row returns empty string (no columns to render)."""
        assert format_table([[]]) == ""


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


class TestCliBarrelExports:
    """Regression tests for #462: the cli package barrel exposes the framework."""

    def test_framework_symbols_importable_from_cli_package(self) -> None:
        """The CLI framework must be reachable via `from hephaestus.cli import ...`."""
        from hephaestus.cli import (  # noqa: F401 - import is the assertion
            COMMAND_REGISTRY,
            Colors,
            CommandRegistry,
            add_json_arg,
            add_logging_args,
            add_version_arg,
            confirm_action,
            create_parser,
            format_output,
            format_table,
            register_command,
        )

    def test_cli_all_lists_framework(self) -> None:
        """hephaestus.cli.__all__ lists the framework symbols, not just Colors."""
        import hephaestus.cli as cli

        symbols = (
            "create_parser",
            "COMMAND_REGISTRY",
            "format_table",
            "Colors",
            "add_version_arg",
        )
        for symbol in symbols:
            assert symbol in cli.__all__
            assert hasattr(cli, symbol)


class TestAddJsonArg:
    """Tests for add_json_arg."""

    def test_adds_json_flag(self) -> None:
        """add_json_arg() registers --json as a bool flag."""
        parser = argparse.ArgumentParser()
        add_json_arg(parser)
        args = parser.parse_args([])
        assert args.json is False
        args = parser.parse_args(["--json"])
        assert args.json is True

    def test_help_text_mentions_machine_readable(self) -> None:
        """Help string explains the flag's purpose."""
        parser = argparse.ArgumentParser()
        add_json_arg(parser)
        help_text = parser.format_help()
        assert "--json" in help_text
        assert "JSON" in help_text


class TestEmitJsonStatus:
    """Tests for emit_json_status."""

    def test_ok_status_on_zero_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        """exit_code=0 emits status='ok'."""
        emit_json_status(0)
        out = json.loads(capsys.readouterr().out)
        assert out == {"status": "ok", "exit_code": 0}

    def test_error_status_on_nonzero_exit(self, capsys: pytest.CaptureFixture[str]) -> None:
        """exit_code != 0 emits status='error'."""
        emit_json_status(2)
        out = json.loads(capsys.readouterr().out)
        assert out == {"status": "error", "exit_code": 2}

    def test_message_included(self, capsys: pytest.CaptureFixture[str]) -> None:
        """An optional message is added to the envelope."""
        emit_json_status(1, message="thing broke")
        out = json.loads(capsys.readouterr().out)
        assert out["message"] == "thing broke"

    def test_extra_fields_merged(self, capsys: pytest.CaptureFixture[str]) -> None:
        """Extra kwargs land as top-level keys."""
        emit_json_status(0, files_checked=5, warnings=0)
        out = json.loads(capsys.readouterr().out)
        assert out["files_checked"] == 5
        assert out["warnings"] == 0
