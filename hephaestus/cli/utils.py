#!/usr/bin/env python3
"""Enhanced CLI utilities for ProjectHephaestus.

This module provides advanced command line interface utilities including
argument parsing, command registration, and output formatting.

Follows development principles:
- KISS: Simple, focused functions
- DRY: Reusable components
- Modularity: Independent, composable units
"""

import argparse
import json
import logging
import math
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from hephaestus._version_lookup import get_version
from hephaestus.constants import AUTOMATION_LOG_FORMAT, LOG_DATEFMT
from hephaestus.utils.helpers import get_repo_root

__version__ = get_version()

__all__ = [
    "COMMAND_REGISTRY",
    "DRY_RUN_HELP_CAVEAT",
    "CommandRegistry",
    "add_advise_timeout_arg",
    "add_agent_timeout_arg",
    "add_dry_run_arg",
    "add_follow_up_timeout_arg",
    "add_git_message_timeout_arg",
    "add_github_throttle_args",
    "add_json_arg",
    "add_learn_timeout_arg",
    "add_logging_args",
    "add_poll_max_wait_arg",
    "add_version_arg",
    "configure_cli_logging",
    "configure_github_throttle_from_args",
    "confirm_action",
    "create_parser",
    "create_validation_parser",
    "emit_json_status",
    "format_output",
    "format_table",
    "register_command",
    "resolve_repo_root",
]


class CommandRegistry:
    """Registry for CLI commands with decorator-based registration."""

    def __init__(self) -> None:
        """Initialize the command registry with an empty commands dict."""
        self.commands: dict[str, dict[str, Any]] = {}

    def register(
        self, name: str, description: str = "", aliases: list[str] | None = None
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a command function via decorator."""

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.commands[name] = {
                "function": func,
                "description": description,
                "aliases": aliases or [],
            }

            # Register aliases
            for alias in aliases or []:
                self.commands[alias] = self.commands[name]

            return func

        return decorator

    def get_command(self, name: str) -> dict[str, Any] | None:
        """Get a registered command info."""
        return self.commands.get(name)


def create_parser(prog_name: str = "hephaestus") -> argparse.ArgumentParser:
    """Create a standardized argument parser with common options.

    Args:
        prog_name: Program name for the parser

    Returns:
        Configured ArgumentParser instance

    """
    parser = argparse.ArgumentParser(
        prog=prog_name,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s command --help     Show help for a specific command
  %(prog)s --version          Show version information
        """.strip(),
    )

    # Add standard options
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    return parser


def add_logging_args(parser: argparse.ArgumentParser) -> None:
    """Add standard logging arguments to parser.

    Args:
        parser: ArgumentParser instance

    """
    logging_group = parser.add_argument_group("logging options")
    logging_group.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    logging_group.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress informational messages"
    )
    logging_group.add_argument("--log-file", help="Log to file instead of stdout")


def add_version_arg(parser: argparse.ArgumentParser) -> None:
    """Add the standard ``--version`` / ``-V`` flag to a CLI parser.

    Every ``hephaestus-*`` console script accepts ``--version`` for version introspection.
    """
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )


def add_json_arg(parser: argparse.ArgumentParser) -> None:
    """Add the standard ``--json`` flag to a CLI parser.

    Every ``hephaestus-*`` console script accepts ``--json`` so output is
    machine-readable for pipelines. Data-returning CLIs emit their structured
    payload via ``format_output(data, "json")``; status-only CLIs should call
    ``emit_json_status()`` to print a minimal ``{"status": ..., "exit_code": ...}``
    envelope on exit.
    """
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output instead of human-readable text",
    )


def create_validation_parser(
    description: str | None = None,
    *,
    include_repo_root: bool = True,
    prog: str | None = None,
    usage: str | None = None,
    epilog: str | None = None,
    formatter_class: type[argparse.HelpFormatter] = argparse.HelpFormatter,
) -> argparse.ArgumentParser:
    """Create a standardized parser for validation-style CLIs.

    Args:
        description: Parser description text.
        include_repo_root: Whether to add the standard ``--repo-root`` option.
        prog: Optional program name override.
        usage: Optional usage string override.
        epilog: Optional parser epilog text.
        formatter_class: Argparse help formatter class.

    Returns:
        Configured ArgumentParser instance with shared validation flags.

    """
    parser = argparse.ArgumentParser(
        prog=prog,
        usage=usage,
        description=description,
        epilog=epilog,
        formatter_class=formatter_class,
    )
    if include_repo_root:
        parser.add_argument(
            "--repo-root",
            type=Path,
            default=None,
            help="Repository root (default: auto-detect)",
        )
    add_json_arg(parser)
    add_version_arg(parser)
    return parser


def resolve_repo_root(args: argparse.Namespace) -> Path:
    """Return the explicit CLI repository root or auto-detect it."""
    return args.repo_root if args.repo_root is not None else get_repo_root()


def configure_cli_logging(*, verbose: bool = False) -> None:
    """Configure standard stderr-safe logging for a ``hephaestus-*`` CLI.

    Centralizes the ``logging.basicConfig(...)`` boilerplate repeated across
    CLI entry points so the log level and format stay consistent. Use this
    in a CLI ``main()`` instead of calling ``logging.basicConfig`` directly.

    Args:
        verbose: When True, set the root level to ``DEBUG``; otherwise ``INFO``.

    """
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=AUTOMATION_LOG_FORMAT,
        datefmt=LOG_DATEFMT,
    )


DRY_RUN_HELP_CAVEAT = (
    "NOTE: Claude is still invoked, so --dry-run still incurs full "
    "Claude API token cost. It is for correctness rehearsal, not cost preview."
)


def add_dry_run_arg(parser: argparse.ArgumentParser, *, prefix: str | None = None) -> None:
    """Add the standard ``--dry-run`` flag with the canonical help-text contract.

    Every ProjectHephaestus CLI that invokes Claude must surface the same
    contract: GitHub/git mutations are suppressed, but Claude is still called
    and tokens are still spent.

    ``prefix`` is an optional CLI-specific lead-in that names the side-effects
    this particular CLI suppresses (e.g. ``"No review comments posted."``).
    It is placed BEFORE the canonical caveat. If ``prefix`` does not end in
    sentence-terminating punctuation, a period and space are appended so the
    concatenation reads cleanly. The canonical caveat is always appended last
    so it cannot be silently dropped (#772).

    Args:
        parser: ArgumentParser instance to add the flag to
        prefix: Optional CLI-specific description of suppressed side-effects

    """
    if prefix:
        trimmed = prefix.rstrip()
        if trimmed and trimmed[-1] not in ".!?":
            trimmed = trimmed + "."
        help_text = f"{trimmed} {DRY_RUN_HELP_CAVEAT}"
    else:
        help_text = DRY_RUN_HELP_CAVEAT
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=help_text,
    )


def _finite_float(value: str) -> float:
    """Parse a finite float for CLI configuration flags."""
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected a finite number, got {value!r}") from exc
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError(f"expected a finite number, got {value!r}")
    return parsed


def _non_negative_float(value: str) -> float:
    """Parse a finite non-negative float."""
    parsed = _finite_float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative number, got {value!r}")
    return parsed


def _positive_float(value: str) -> float:
    """Parse a finite positive float."""
    parsed = _finite_float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive number, got {value!r}")
    return parsed


def _at_least_one_float(value: str) -> float:
    """Parse a finite float greater than or equal to one."""
    parsed = _finite_float(value)
    if parsed < 1.0:
        raise argparse.ArgumentTypeError(f"expected a number >= 1.0, got {value!r}")
    return parsed


def add_github_throttle_args(parser: argparse.ArgumentParser) -> None:
    """Add GitHub global-throttle configuration flags to a CLI parser."""
    group = parser.add_argument_group("GitHub throttle options")
    group.add_argument(
        "--gh-global-rate",
        type=_non_negative_float,
        default=10.0,
        metavar="FLOAT",
        help=(
            "Global gh token-bucket refill rate in calls/sec (default: 10.0). "
            "Pass 0 to disable the global throttle."
        ),
    )
    group.add_argument(
        "--gh-global-burst",
        type=_at_least_one_float,
        default=30.0,
        metavar="FLOAT",
        help="Global gh token-bucket burst size (default: 30.0).",
    )


def configure_github_throttle_from_args(args: argparse.Namespace) -> None:
    """Apply GitHub global-throttle options parsed from CLI args."""
    from hephaestus.github.rate_limit import configure_gh_global_throttle

    configure_gh_global_throttle(
        rate=float(args.gh_global_rate),
        burst=float(args.gh_global_burst),
    )


def emit_json_status(exit_code: int, message: str | None = None, **extra: Any) -> None:
    """Print a minimal JSON status envelope to stdout.

    Use this in CLIs whose output is just status (e.g. fix/format/install).
    Data-returning CLIs should instead call ``format_output(data, "json")``.

    Args:
        exit_code: The CLI's exit code (0 = ok, non-zero = error).
        message: Optional human-readable summary.
        **extra: Additional fields to merge into the envelope.

    """
    envelope: dict[str, Any] = {
        "status": "ok" if exit_code == 0 else "error",
        "exit_code": exit_code,
    }
    if message is not None:
        envelope["message"] = message
    envelope.update(extra)
    print(json.dumps(envelope))


def confirm_action(
    prompt: str = "Are you sure?", default: bool = False, max_attempts: int = 3
) -> bool:
    """Prompt user for confirmation.

    Args:
        prompt: Confirmation prompt
        default: Default response if user just presses Enter
        max_attempts: Maximum number of invalid-input retries before returning default

    Returns:
        User's confirmation decision

    """
    choices = "Y/n" if default else "y/N"
    for _ in range(max_attempts):
        try:
            choice = input(f"{prompt} [{choices}] ").strip().lower()
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            sys.exit(1)

        if not choice:
            return default
        elif choice in ["y", "yes"]:
            return True
        elif choice in ["n", "no"]:
            return False
        else:
            print("Invalid choice. Please enter 'y' or 'n'.")
    return default


def format_table(
    rows: Sequence[Sequence[str]], headers: Sequence[str] | None = None, separator: str = "  "
) -> str:
    """Format data as a pretty table.

    Args:
        rows: Table data rows
        headers: Optional header row
        separator: Column separator

    Returns:
        Formatted table string

    """
    # Combine headers and rows
    all_rows = [headers] if headers else []
    all_rows.extend(rows)

    if not all_rows:
        return ""

    # Normalize ragged rows: pad short rows with "" to the max column count.
    num_cols = max((len(row) for row in all_rows), default=0)
    if num_cols == 0:
        return ""
    normalized = [[str(cell) for cell in row] + [""] * (num_cols - len(row)) for row in all_rows]

    # Calculate column widths from the normalized matrix.
    col_widths = [max(len(row[i]) for row in normalized) for i in range(num_cols)]

    # Format rows
    result = []
    for row_idx, row in enumerate(normalized):
        formatted_row = separator.join(cell.ljust(col_widths[i]) for i, cell in enumerate(row))
        result.append(formatted_row)

        # Add separator line after headers
        if headers and row_idx == 0:
            separator_line = separator.join("-" * width for width in col_widths)
            result.append(separator_line)

    return "\n".join(result)


def format_output(data: Any, format_type: str = "text") -> str:
    """Format output in various formats.

    Args:
        data: Data to format
        format_type: Output format. One of ``"json"``, ``"table"``, or
            ``"text"`` (the default). The match is exact and case-sensitive
            (e.g. ``"JSON"`` is NOT recognized and falls back to ``"text"``).
            ``"table"`` applies only when ``data`` is a list or tuple; for any
            other ``data`` type a ``"table"`` request falls back to ``"text"``.
            Any unrecognized ``format_type`` (a typo, ``""``, etc.) also falls
            back to the ``"text"`` format rather than raising — callers wanting
            strict validation must check ``format_type`` before calling.

    Returns:
        Formatted string representation

    """
    if format_type == "json":
        return json.dumps(data, indent=2)
    elif format_type == "table" and isinstance(data, (list, tuple)):
        if data and isinstance(data[0], dict):
            # Dict rows to table
            headers = list(data[0].keys()) if data else []
            rows = [[str(row.get(h, "")) for h in headers] for row in data]
            return format_table(rows, headers)
        elif data and isinstance(data[0], (list, tuple)):
            # Already in row format
            return format_table(data)
        else:
            # Simple list
            return "\n".join(str(item) for item in data)
    else:
        # Default text format
        if isinstance(data, (list, tuple)):
            return "\n".join(str(item) for item in data)
        elif isinstance(data, dict):
            lines = []
            for key, value in data.items():
                lines.append(f"{key}: {value}")
            return "\n".join(lines)
        else:
            return str(data)


# Global command registry
COMMAND_REGISTRY = CommandRegistry()


def register_command(
    name: str, description: str = "", aliases: list[str] | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register a CLI command via decorator.

    Args:
        name: Command name
        description: Brief command description
        aliases: Optional command aliases

    """
    return COMMAND_REGISTRY.register(name, description, aliases)


def add_agent_timeout_arg(
    parser: argparse.ArgumentParser,
    *,
    flag: str = "--agent-timeout",
    dest: str = "agent_timeout",
    default_doc: int = 7200,
    help_extra: str = "",
) -> None:
    """Add an optional agent subprocess timeout flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to
        flag: The CLI flag name (default: ``--agent-timeout``)
        dest: The argparse destination attribute (default: ``agent_timeout``)
        default_doc: Default value shown in help text (default: 7200)
        help_extra: Optional extra help text appended after the default note

    """
    extra = f" {help_extra}" if help_extra else ""
    parser.add_argument(
        flag,
        dest=dest,
        type=int,
        default=None,
        metavar="SECONDS",
        help=f"Agent subprocess timeout in seconds (default: {default_doc}).{extra}",
    )


def add_advise_timeout_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--advise-timeout`` flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to

    """
    parser.add_argument(
        "--advise-timeout",
        dest="advise_timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Timeout for the advise sub-agent in seconds (default: 7200).",
    )


def add_poll_max_wait_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--poll-max-wait`` flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to

    """
    parser.add_argument(
        "--poll-max-wait",
        dest="poll_max_wait",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Max wall-clock seconds to poll CI before backing off (default: 600).",
    )


def add_git_message_timeout_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--git-message-timeout`` flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to

    """
    parser.add_argument(
        "--git-message-timeout",
        dest="git_message_timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Timeout for the lightweight commit/PR message agent (default: 300).",
    )


def add_learn_timeout_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--learn-timeout`` flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to

    """
    parser.add_argument(
        "--learn-timeout",
        dest="learn_timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Timeout for the /learn agent session (default: 7200).",
    )


def add_follow_up_timeout_arg(parser: argparse.ArgumentParser) -> None:
    """Add ``--follow-up-timeout`` flag to a CLI parser.

    Args:
        parser: ArgumentParser instance to add the flag to

    """
    parser.add_argument(
        "--follow-up-timeout",
        dest="follow_up_timeout",
        type=int,
        default=None,
        metavar="SECONDS",
        help="Timeout for the follow-up-issue agent session (default: 7200).",
    )
