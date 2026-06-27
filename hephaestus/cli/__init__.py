"""Command-line interface tools."""

from hephaestus.cli.colors import Colors
from hephaestus.cli.utils import (
    COMMAND_REGISTRY,
    DRY_RUN_HELP_CAVEAT,
    CommandRegistry,
    add_dry_run_arg,
    add_github_throttle_args,
    add_json_arg,
    add_logging_args,
    add_version_arg,
    configure_github_throttle_from_args,
    confirm_action,
    create_parser,
    create_validation_parser,
    emit_json_status,
    format_output,
    format_table,
    register_command,
    resolve_repo_root,
)

__all__ = [
    "COMMAND_REGISTRY",
    "DRY_RUN_HELP_CAVEAT",
    "Colors",
    "CommandRegistry",
    "add_dry_run_arg",
    "add_github_throttle_args",
    "add_json_arg",
    "add_logging_args",
    "add_version_arg",
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
