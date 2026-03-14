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
import sys
from collections.abc import Callable, Sequence
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

try:
    __version__ = _pkg_version("hephaestus")
except PackageNotFoundError:
    __version__ = "unknown"


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


def confirm_action(prompt: str = "Are you sure?", default: bool = False) -> bool:
    """Prompt user for confirmation.

    Args:
        prompt: Confirmation prompt
        default: Default response if user just presses Enter

    Returns:
        User's confirmation decision

    """
    choices = "Y/n" if default else "y/N"
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
        return confirm_action(prompt, default)


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

    # Calculate column widths
    col_widths = [
        max(len(str(row[i])) for row in all_rows if i < len(row))
        for i in range(max(len(row) for row in all_rows))
    ]

    # Handle case where there are no columns
    if not col_widths:
        return ""

    # Format rows
    result = []
    for row_idx, row in enumerate(all_rows):
        formatted_row = separator.join(
            str(cell).ljust(col_widths[i]) for i, cell in enumerate(row) if i < len(col_widths)
        )
        result.append(formatted_row)

        # Add separator line after headers
        if headers and row_idx == 0 and col_widths:
            separator_line = separator.join("-" * width for width in col_widths)
            result.append(separator_line)

    return "\n".join(result)


def format_output(data: Any, format_type: str = "text") -> str:
    """Format output in various formats.

    Args:
        data: Data to format
        format_type: Output format ('text', 'json', 'table')

    Returns:
        Formatted string representation

    """
    if format_type == "json":
        import json

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
