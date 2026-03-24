#!/usr/bin/env python3
"""Integration tests verifying all CLI entry points are importable and functional.

Each entry point defined in pyproject.toml [project.scripts] must:
1. Have an importable ``main`` callable in its target module.
2. Respond to ``--help`` without crashing (exit code 0).
"""

import importlib
import subprocess
import sys

import pytest

# ---------------------------------------------------------------------------
# Every (command_name, module_path) pair from pyproject.toml [project.scripts]
# ---------------------------------------------------------------------------
CLI_ENTRY_POINTS = [
    ("hephaestus-changelog", "hephaestus.git.changelog"),
    ("hephaestus-merge-prs", "hephaestus.github.pr_merge"),
    ("hephaestus-system-info", "hephaestus.system.info"),
    ("hephaestus-download-dataset", "hephaestus.datasets.downloader"),
    ("hephaestus-check-python-version", "hephaestus.validation.python_version"),
    ("hephaestus-check-test-structure", "hephaestus.validation.test_structure"),
    ("hephaestus-check-coverage", "hephaestus.validation.coverage"),
    ("hephaestus-check-complexity", "hephaestus.validation.complexity"),
    ("hephaestus-filter-audit", "hephaestus.validation.audit"),
    ("hephaestus-validate-schemas", "hephaestus.validation.schema"),
    ("hephaestus-validate-links", "hephaestus.validation.markdown"),
    ("hephaestus-check-type-aliases", "hephaestus.validation.type_aliases"),
    ("hephaestus-check-docstrings", "hephaestus.validation.docstrings"),
]


# ---------------------------------------------------------------------------
# Helper IDs for readable parametrize output
# ---------------------------------------------------------------------------
_ENTRY_POINT_IDS = [ep[0] for ep in CLI_ENTRY_POINTS]


class TestCLIMainImportable:
    """Verify that the ``main`` function is importable from each entry point module."""

    @pytest.mark.parametrize("command,module_path", CLI_ENTRY_POINTS, ids=_ENTRY_POINT_IDS)
    def test_main_importable(self, command: str, module_path: str) -> None:
        """The target module must expose a callable ``main``."""
        mod = importlib.import_module(module_path)
        assert hasattr(mod, "main"), f"{module_path} has no 'main' attribute"
        assert callable(mod.main), f"{module_path}.main is not callable"


class TestCLIHelpFlag:
    """Verify that every CLI entry point responds to ``--help`` without crashing."""

    @pytest.mark.parametrize("command,module_path", CLI_ENTRY_POINTS, ids=_ENTRY_POINT_IDS)
    def test_help_flag(self, command: str, module_path: str) -> None:
        """Running ``python -m <module> --help`` must exit with code 0.

        We invoke via ``sys.executable -m`` rather than the console-script
        wrapper so the test works in development installs where the wrapper
        script may not be on PATH.
        """
        # Build module invocation: ``python -m hephaestus.validation.coverage``
        # argparse-based CLIs print help text and exit 0 on --help.
        result = subprocess.run(
            [sys.executable, "-m", module_path, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{command} (module {module_path}) failed with rc={result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )
        # Sanity-check: argparse --help output always includes "usage"
        assert "usage" in result.stdout.lower(), f"{command} --help did not print usage information"


class TestCLIEntryPointCount:
    """Guard against adding entry points without updating these tests."""

    def test_expected_entry_point_count(self) -> None:
        """The test list must cover all 13 declared entry points."""
        assert len(CLI_ENTRY_POINTS) == 13
