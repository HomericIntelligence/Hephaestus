"""ProjectHephaestus - Centralized utility library for HomericIntelligence ecosystem."""

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    __version__ = _pkg_version("hephaestus")
except PackageNotFoundError:
    __version__ = "0.3.0"

__author__ = "Micah Villmow"

# Import from utils (consolidated location)
from .cli.utils import (
    COMMAND_REGISTRY,
    add_logging_args,
    confirm_action,
    create_parser,
    format_output,
    format_table,
    register_command,
)
from .config.utils import get_config_value, get_setting, load_config, merge_configs
from .io.utils import ensure_directory, load_data, read_file, save_data, write_file
from .utils import (
    flatten_dict,
    get_proj_root,
    get_repo_root,
    human_readable_size,
    install_package,
    retry_with_backoff,
    run_subprocess,
    slugify,
)

__all__ = [
    "COMMAND_REGISTRY",
    "__version__",
    "add_logging_args",
    "confirm_action",
    "create_parser",
    "ensure_directory",
    "flatten_dict",
    "format_output",
    "format_table",
    "get_config_value",
    "get_proj_root",
    "get_repo_root",
    "get_setting",
    "human_readable_size",
    "install_package",
    "load_config",
    "load_data",
    "merge_configs",
    "read_file",
    "register_command",
    "retry_with_backoff",
    "run_subprocess",
    "save_data",
    "slugify",
    "write_file",
]
