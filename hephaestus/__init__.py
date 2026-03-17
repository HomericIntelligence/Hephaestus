"""ProjectHephaestus - Centralized utility library for HomericIntelligence ecosystem."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

try:
    __version__ = _pkg_version("hephaestus")
except PackageNotFoundError:
    __version__ = "unknown"

__author__ = "Micah Villmow"

# Public API surface — prefer subpackage imports for full access:
#   from hephaestus.utils import slugify
#   from hephaestus.io.utils import load_data
#
# Design note: __all__ lists the *recommended* top-level symbols (9 most-used).
# _LAZY_IMPORTS maps the full set of lazily-loaded symbols (28 total) that are
# also accessible via `hephaestus.<name>` but not re-exported by star-import.
# This keeps `import hephaestus` fast (PEP 562) while providing convenient access.
__all__ = [
    "ContextLogger",
    "__version__",
    "ensure_directory",
    "get_logger",
    "get_system_info",
    "load_config",
    "retry_with_backoff",
    "setup_logging",
    "slugify",
]

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    # name -> (module, attr)
    "COMMAND_REGISTRY": ("hephaestus.cli.utils", "COMMAND_REGISTRY"),
    "add_logging_args": ("hephaestus.cli.utils", "add_logging_args"),
    "confirm_action": ("hephaestus.cli.utils", "confirm_action"),
    "create_parser": ("hephaestus.cli.utils", "create_parser"),
    "format_output": ("hephaestus.cli.utils", "format_output"),
    "format_table": ("hephaestus.cli.utils", "format_table"),
    "register_command": ("hephaestus.cli.utils", "register_command"),
    "get_config_value": ("hephaestus.config.utils", "get_config_value"),
    "get_setting": ("hephaestus.config.utils", "get_setting"),
    "load_config": ("hephaestus.config.utils", "load_config"),
    "merge_configs": ("hephaestus.config.utils", "merge_configs"),
    "ensure_directory": ("hephaestus.io.utils", "ensure_directory"),
    "load_data": ("hephaestus.io.utils", "load_data"),
    "read_file": ("hephaestus.io.utils", "read_file"),
    "safe_write": ("hephaestus.io.utils", "safe_write"),
    "save_data": ("hephaestus.io.utils", "save_data"),
    "write_file": ("hephaestus.io.utils", "write_file"),
    "write_secure": ("hephaestus.io.utils", "write_secure"),
    "detect_rate_limit": ("hephaestus.github.rate_limit", "detect_rate_limit"),
    "parse_reset_epoch": ("hephaestus.github.rate_limit", "parse_reset_epoch"),
    "wait_until": ("hephaestus.github.rate_limit", "wait_until"),
    "ContextLogger": ("hephaestus.logging.utils", "ContextLogger"),
    "get_logger": ("hephaestus.logging.utils", "get_logger"),
    "setup_logging": ("hephaestus.logging.utils", "setup_logging"),
    "format_system_info": ("hephaestus.system.info", "format_system_info"),
    "get_system_info": ("hephaestus.system.info", "get_system_info"),
    "flatten_dict": ("hephaestus.utils", "flatten_dict"),
    "get_proj_root": ("hephaestus.utils", "get_proj_root"),
    "get_repo_root": ("hephaestus.utils", "get_repo_root"),
    "human_readable_size": ("hephaestus.utils", "human_readable_size"),
    "install_package": ("hephaestus.utils", "install_package"),
    "retry_with_backoff": ("hephaestus.utils", "retry_with_backoff"),
    "run_subprocess": ("hephaestus.utils", "run_subprocess"),
    "slugify": ("hephaestus.utils", "slugify"),
}


def __getattr__(name: str) -> Any:
    """Lazy-load public symbols on first access (PEP 562)."""
    if name in _LAZY_IMPORTS:
        module_name, attr = _LAZY_IMPORTS[name]
        import importlib

        module = importlib.import_module(module_name)
        value = getattr(module, attr)
        # Cache in module globals to avoid repeated lookups
        globals()[name] = value
        return value
    raise AttributeError(f"module 'hephaestus' has no attribute {name!r}")
