#!/usr/bin/env python3
"""Integration tests verifying all public symbols in __all__ are importable."""

import importlib
import pytest


# All top-level symbols from hephaestus.__all__
TOP_LEVEL_SYMBOLS = [
    "COMMAND_REGISTRY",
    "__version__",
    "ContextLogger",
    "add_logging_args",
    "confirm_action",
    "create_parser",
    "ensure_directory",
    "flatten_dict",
    "format_output",
    "format_system_info",
    "format_table",
    "get_config_value",
    "get_logger",
    "get_proj_root",
    "get_repo_root",
    "get_setting",
    "get_system_info",
    "human_readable_size",
    "install_package",
    "load_config",
    "load_data",
    "log_context",
    "merge_configs",
    "read_file",
    "register_command",
    "retry_with_backoff",
    "run_subprocess",
    "safe_write",
    "save_data",
    "setup_logging",
    "slugify",
    "write_file",
]

SUBPACKAGE_SYMBOLS = [
    ("hephaestus.io", ["read_file", "write_file", "safe_write", "load_data", "save_data", "ensure_directory"]),
    ("hephaestus.logging", ["setup_logging", "get_logger", "ContextLogger", "log_context"]),
    ("hephaestus.system", ["get_system_info", "format_system_info"]),
    ("hephaestus.datasets", ["DatasetDownloader"]),
    ("hephaestus.github", ["detect_repo_from_remote", "local_branch_exists", "merge_prs"]),
    ("hephaestus.config", ["load_config", "get_setting", "get_config_value", "merge_configs"]),
    ("hephaestus.git", ["categorize_commits", "generate_changelog", "parse_commit"]),
    ("hephaestus.cli", ["Colors"]),
    ("hephaestus.utils", ["slugify", "retry_with_backoff", "flatten_dict", "get_repo_root"]),
    ("hephaestus.markdown", ["MarkdownFixer", "LinkFixer"]),
    ("hephaestus.benchmarks", ["detect_regressions", "load_benchmark_results"]),
    ("hephaestus.version", ["VersionManager", "parse_version"]),
    ("hephaestus.validation", ["ReadmeValidator"]),
]


class TestTopLevelImports:
    """Verify all top-level __all__ symbols are importable."""

    def test_package_importable(self):
        """The hephaestus package itself must be importable."""
        import hephaestus  # noqa: F401

    def test_version_defined(self):
        """__version__ must be defined and non-empty."""
        import hephaestus
        assert hephaestus.__version__
        assert isinstance(hephaestus.__version__, str)

    @pytest.mark.parametrize("symbol", TOP_LEVEL_SYMBOLS)
    def test_top_level_symbol_importable(self, symbol):
        """Each symbol in __all__ must be accessible from the top-level package."""
        mod = importlib.import_module("hephaestus")
        assert hasattr(mod, symbol), f"hephaestus.{symbol} not found"

    def test_all_declared(self):
        """hephaestus.__all__ must be defined and non-empty."""
        import hephaestus
        assert hasattr(hephaestus, "__all__")
        assert len(hephaestus.__all__) > 0


class TestSubpackageImports:
    """Verify each subpackage exports its public symbols."""

    @pytest.mark.parametrize("package,symbols", SUBPACKAGE_SYMBOLS)
    def test_subpackage_symbols(self, package, symbols):
        """Each subpackage must export its expected public symbols."""
        mod = importlib.import_module(package)
        for symbol in symbols:
            assert hasattr(mod, symbol), f"{package}.{symbol} not found"

    def test_io_functions_callable(self):
        """Core io functions must be callable."""
        from hephaestus.io import read_file, write_file, ensure_directory
        assert callable(read_file)
        assert callable(write_file)
        assert callable(ensure_directory)

    def test_logging_functions_callable(self):
        """Core logging functions must be callable."""
        from hephaestus.logging import setup_logging, get_logger
        assert callable(setup_logging)
        assert callable(get_logger)

    def test_slugify_works(self):
        """slugify must produce correct output (smoke test)."""
        from hephaestus.utils import slugify
        assert slugify("Hello World") == "hello-world"
        assert slugify("foo_bar.baz") == "foo-bar-baz"

    def test_retry_with_backoff_is_decorator(self):
        """retry_with_backoff must return a decorator."""
        from hephaestus.utils import retry_with_backoff
        decorator = retry_with_backoff(max_retries=1, initial_delay=0.0)
        assert callable(decorator)

        @decorator
        def noop():
            return 42

        assert noop() == 42
