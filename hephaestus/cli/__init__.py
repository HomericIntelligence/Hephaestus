"""Command-line interface tools."""

from hephaestus.cli.colors import Colors
from hephaestus.cli.utils import (
    COMMAND_REGISTRY,
    CommandRegistry,
    add_json_arg,
    add_logging_args,
    add_version_arg,
    confirm_action,
    create_parser,
    emit_json_status,
    format_output,
    format_table,
    register_command,
)

__all__ = [
    "COMMAND_REGISTRY",
    "Colors",
    "CommandRegistry",
    "add_json_arg",
    "add_logging_args",
    "add_version_arg",
    "confirm_action",
    "create_parser",
    "emit_json_status",
    "format_output",
    "format_table",
    "register_command",
]
