"""ProjectHephaestus - Centralized utility library for HomericIntelligence ecosystem."""

# Import from the lowest common layer first
from .helpers import slugify, retry_with_backoff, human_readable_size, flatten_dict
from .config.utils import get_setting, load_config, merge_configs, get_config_value
from .io.utils import read_file, write_file, load_data, save_data, ensure_directory
from .cli.utils import (
    create_parser,
    add_logging_args,
    confirm_action,
    format_table,
    format_output,
    register_command,
    COMMAND_REGISTRY
)

__version__ = "0.1.0"
__author__ = "HomericIntelligence Team"

__all__ = [
    "slugify",
    "retry_with_backoff",
    "human_readable_size", 
    "flatten_dict",
    "get_setting",
    "load_config",
    "merge_configs",
    "get_config_value",
    "read_file",
    "write_file",
    "load_data",
    "save_data",
    "ensure_directory",
    "create_parser",
    "add_logging_args",
    "confirm_action",
    "format_table",
    "format_output",
    "register_command",
    "COMMAND_REGISTRY"
]
