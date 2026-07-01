#!/usr/bin/env python3
"""Tests for CLI utilities."""

import argparse
import json
import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.cli.utils import (
    CommandRegistry,
    add_github_throttle_args,
    add_json_arg,
    add_logging_args,
    add_version_arg,
    configure_cli_logging,
    configure_github_throttle_from_args,
    confirm_action,
    create_parser,
    create_validation_parser,
    emit_json_status,
    format_output,
    format_table,
    resolve_repo_root,
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

    def test_accepts_description_epilog_and_usage(self) -> None:
        """Parser metadata can be preserved while using the shared factory."""
        parser = create_parser(
            "demo",
            description="Demo description",
            epilog="Demo epilog",
            usage="demo [options]",
        )

        assert parser.description == "Demo description"
        assert parser.epilog == "Demo epilog"
        assert parser.usage == "demo [options]"

    def test_can_suppress_default_epilog(self) -> None:
        """Call sites can opt out of the shared examples epilog."""
        parser = create_parser("demo", epilog=None)

        assert parser.epilog is None


class TestCreateValidationParser:
    """Tests for create_validation_parser."""

    def test_default_flags_present(self) -> None:
        """Parser includes --repo-root, --json, and --version by default."""
        parser = create_validation_parser("demo")
        args = parser.parse_args([])
        assert args.repo_root is None
        assert args.json is False

    def test_explicit_repo_root_captured(self, tmp_path: Path) -> None:
        """Explicit --repo-root is captured as a Path in the namespace."""
        parser = create_validation_parser("demo")
        args = parser.parse_args(["--repo-root", str(tmp_path)])
        assert args.repo_root == tmp_path

    def test_include_repo_root_false_omits_flag(self) -> None:
        """include_repo_root=False suppresses the --repo-root flag."""
        parser = create_validation_parser("demo", include_repo_root=False)
        args = parser.parse_args([])
        assert not hasattr(args, "repo_root")

    def test_parse_known_args_preserves_unknown_flags(self, tmp_path: Path) -> None:
        """parse_known_args() does not consume passthrough args."""
        parser = create_validation_parser("demo")
        args, unknown = parser.parse_known_args(["--json", "--strict-equality", "pkg/a.py"])
        assert args.json is True
        assert unknown == ["--strict-equality", "pkg/a.py"]


class TestConfigureCliLogging:
    """Tests for configure_cli_logging."""

    def test_default_uses_info_level(self) -> None:
        """Without verbose, basicConfig is called at INFO level."""
        with patch("hephaestus.cli.utils.logging.basicConfig") as basic_config:
            configure_cli_logging()
        assert basic_config.call_args.kwargs["level"] == logging.INFO

    def test_verbose_uses_debug_level(self) -> None:
        """verbose=True raises the configured level to DEBUG."""
        with patch("hephaestus.cli.utils.logging.basicConfig") as basic_config:
            configure_cli_logging(verbose=True)
        assert basic_config.call_args.kwargs["level"] == logging.DEBUG

    def test_sets_standard_format(self) -> None:
        """A consistent stderr-safe format string is applied."""
        with patch("hephaestus.cli.utils.logging.basicConfig") as basic_config:
            configure_cli_logging()
        assert "%(levelname)s" in basic_config.call_args.kwargs["format"]
        assert "%(name)s" in basic_config.call_args.kwargs["format"]

    def test_routes_through_canonical_constants(self) -> None:
        """configure_cli_logging uses the shared AUTOMATION_LOG_FORMAT (#1427)."""
        import hephaestus.constants as constants

        with patch("hephaestus.cli.utils.logging.basicConfig") as basic_config:
            configure_cli_logging()
        assert basic_config.call_args.kwargs["format"] == constants.AUTOMATION_LOG_FORMAT
        assert basic_config.call_args.kwargs["datefmt"] == constants.LOG_DATEFMT


class TestResolveRepoRoot:
    """Tests for resolve_repo_root."""

    def test_explicit_root_returned_directly(self, tmp_path: Path) -> None:
        """resolve_repo_root returns the explicit --repo-root without discovery."""
        parser = create_validation_parser("demo")
        args = parser.parse_args(["--repo-root", str(tmp_path)])
        with patch("hephaestus.utils.helpers.get_repo_root") as get_root:
            result = resolve_repo_root(args)
        assert result == tmp_path
        get_root.assert_not_called()

    def test_auto_detect_when_not_provided(self, tmp_path: Path) -> None:
        """resolve_repo_root falls back to get_repo_root() when --repo-root is absent."""
        parser = create_validation_parser("demo")
        args = parser.parse_args([])
        with patch("hephaestus.cli.utils.get_repo_root", return_value=tmp_path):
            result = resolve_repo_root(args)
        assert result == tmp_path


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

    def test_invalid_format_falls_back_to_text(self) -> None:
        """Unrecognized format_type renders as text, not an error (POLA #1509).

        Non-vacuous: if the else-branch text fallback (cli/utils.py:380-388)
        raised instead, the bogus/yaml/"" cases would fail; the "JSON" case
        guards format_output's case-SENSITIVITY (== "json", not .lower()).
        """
        data = {"name": "hephaestus", "version": "0.3.0"}
        for bogus in ("bogus", "yaml", "", "JSON"):  # "JSON" stays case-sensitive
            result = format_output(data, format_type=bogus)
            assert "name: hephaestus" in result  # text branch ran
            assert "{" not in result  # NOT json output

    def test_table_format_on_dict_falls_back_to_text(self) -> None:
        """'table' on non-sequence data falls back to text, not an error (#1509).

        Non-vacuous: if the isinstance(data, (list, tuple)) guard at
        cli/utils.py:368 were dropped, a dict would hit the table branch and
        format differently; asserting the text 'key: value' line AND absence of
        json's '{' pins the text path specifically.
        """
        data = {"name": "hephaestus", "version": "0.3.0"}
        result = format_output(data, format_type="table")
        assert "name: hephaestus" in result  # text branch, not table
        assert "{" not in result  # NOT json output


class TestCliBarrelExports:
    """Regression tests for #462: the cli package barrel exposes the framework."""

    def test_framework_symbols_importable_from_cli_package(self) -> None:
        """The CLI framework must be reachable via `from hephaestus.cli import ...`."""
        import hephaestus.cli as cli

        # Verify symbols are accessible from the package
        assert hasattr(cli, "COMMAND_REGISTRY")
        assert hasattr(cli, "Colors")
        assert hasattr(cli, "CommandRegistry")
        assert hasattr(cli, "add_json_arg")
        assert hasattr(cli, "add_logging_args")
        assert hasattr(cli, "add_version_arg")
        assert hasattr(cli, "confirm_action")
        assert hasattr(cli, "create_parser")
        assert hasattr(cli, "format_output")
        assert hasattr(cli, "format_table")
        assert hasattr(cli, "register_command")

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

    def test_cli_all_covers_module_all(self) -> None:
        """Package __all__ re-exports every symbol cli.utils.__all__ declares (#1511)."""
        import hephaestus.cli as cli

        utils = cli.utils

        missing = set(utils.__all__) - set(cli.__all__)
        assert not missing, f"cli.__all__ omits stable utils symbols: {sorted(missing)}"
        for symbol in utils.__all__:
            assert hasattr(cli, symbol), f"cli has no attribute {symbol!r}"

    def test_dry_run_symbols_reexport_identity(self) -> None:
        """Re-exports are the SAME objects so patch paths don't diverge (#1511)."""
        import hephaestus.cli as cli

        utils = cli.utils

        assert cli.add_dry_run_arg is utils.add_dry_run_arg
        assert cli.DRY_RUN_HELP_CAVEAT is utils.DRY_RUN_HELP_CAVEAT


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
