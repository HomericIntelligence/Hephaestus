#!/usr/bin/env python3
"""Integration tests verifying all public symbols in __all__ are importable."""

import importlib
import warnings

import pytest

pytestmark = pytest.mark.integration

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
    "get_logger",
    "get_proj_root",
    "get_repo_root",
    "get_setting",
    "get_system_info",
    "human_readable_size",
    "install_package",
    "load_config",
    "load_data",
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

# Deprecated symbols removed in issue #1420. Guards against re-introduction at
# the top-level package surface.
REMOVED_DEPRECATED_SYMBOLS = ("get_config_value", "retry_with_jitter")

SUBPACKAGE_SYMBOLS = [
    (
        "hephaestus.io",
        ["read_file", "write_file", "safe_write", "load_data", "save_data", "ensure_directory"],
    ),
    ("hephaestus.logging", ["setup_logging", "get_logger", "ContextLogger", "JsonFormatter"]),
    ("hephaestus.system", ["get_system_info", "format_system_info"]),
    ("hephaestus.datasets", ["DatasetDownloader"]),
    ("hephaestus.github", ["detect_repo_from_remote", "local_branch_exists", "collect_stats"]),
    ("hephaestus.config", ["load_config", "get_setting", "merge_configs"]),
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

    def test_version_resolves_against_installed_distribution(self):
        """__version__ must resolve from distribution metadata, not fall back to 'unknown'.

        Regression test for #433: importlib.metadata.version() must be called with the
        PyPI distribution name ("HomericIntelligence-Hephaestus"), not the import name
        ("hephaestus"), which it does not normalize to the same key.
        """
        from importlib.metadata import PackageNotFoundError, version as pkg_version

        import hephaestus

        try:
            expected = pkg_version("HomericIntelligence-Hephaestus")
        except PackageNotFoundError:
            pytest.skip(
                "package metadata unavailable; run `pixi run dev-install` before direct pytest "
                "invocations, or use the self-contained `pixi run test tests/integration`"
            )

        assert hephaestus.__version__ == expected
        assert hephaestus.__version__ != "unknown"

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

    @pytest.mark.parametrize("symbol", REMOVED_DEPRECATED_SYMBOLS)
    def test_removed_deprecated_top_level_symbols_unavailable(self, symbol):
        """Removed deprecated symbols (#1420) must be absent from the top-level surface."""
        import hephaestus

        hephaestus.__dict__.pop(symbol, None)  # bust any PEP 562 cache
        assert symbol not in hephaestus._LAZY_IMPORTS
        assert symbol not in dir(hephaestus)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            assert not hasattr(hephaestus, symbol)


class TestDirDiscoverability:
    """Verify dir(hephaestus) exposes the lazy public surface (PEP 562, #1512)."""

    def test_dir_lists_all_lazy_symbols(self):
        """Every lazily-loaded symbol must be visible to dir()."""
        import hephaestus

        listed = set(dir(hephaestus))
        missing = set(hephaestus._LAZY_IMPORTS) - listed
        assert not missing, f"lazy symbols invisible to dir(): {sorted(missing)}"

    def test_dir_lists_all_public_api(self):
        """Every __all__ entry must be visible to dir()."""
        import hephaestus

        listed = set(dir(hephaestus))
        missing = set(hephaestus.__all__) - listed
        assert not missing, f"__all__ entries invisible to dir(): {sorted(missing)}"

    def test_dir_preserves_existing_attributes(self):
        """dir() must not drop already-visible module attributes."""
        import hephaestus

        listed = set(dir(hephaestus))
        assert {"__version__", "__author__"} <= listed

    def test_dir_does_not_import_or_warn(self):
        """dir() returns names only — no lazy import, no DeprecationWarning."""
        import hephaestus

        hephaestus.__dict__.pop("retry_with_backoff", None)  # bust PEP 562 cache
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            _ = dir(hephaestus)

        assert not [w for w in caught if issubclass(w.category, DeprecationWarning)], (
            "dir() must not trigger deprecation warnings"
        )
        assert "retry_with_backoff" not in hephaestus.__dict__, (
            "dir() must not eagerly resolve lazy symbols into module globals"
        )


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
        from hephaestus.io import ensure_directory, read_file, write_file

        assert callable(read_file)
        assert callable(write_file)
        assert callable(ensure_directory)

    def test_logging_functions_callable(self):
        """Core logging functions must be callable."""
        from hephaestus.logging import get_logger, setup_logging

        assert callable(setup_logging)
        assert callable(get_logger)

    def test_slugify_works(self):
        """Slugify must produce correct output (smoke test)."""
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
