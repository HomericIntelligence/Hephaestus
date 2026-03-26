#!/usr/bin/env python3
"""Integration tests verifying CLI entry points are importable and respond to --help."""

import importlib
import subprocess
import sys

import pytest

# Entry point modules and their main() functions, matching pyproject.toml [project.scripts]
ENTRY_POINTS = [
    ("hephaestus.git.changelog", "main"),
    ("hephaestus.github.pr_merge", "main"),
    ("hephaestus.system.info", "main"),
    ("hephaestus.datasets.downloader", "main"),
]


class TestCLIEntryPointImports:
    """Verify each CLI entry point's main() function is importable and callable."""

    @pytest.mark.parametrize(
        "module_path,func_name",
        ENTRY_POINTS,
        ids=[
            "hephaestus-changelog",
            "hephaestus-merge-prs",
            "hephaestus-system-info",
            "hephaestus-download-dataset",
        ],
    )
    def test_main_importable_and_callable(self, module_path: str, func_name: str) -> None:
        """Each entry point's main() must be importable and callable."""
        mod = importlib.import_module(module_path)
        main_func = getattr(mod, func_name, None)
        assert main_func is not None, f"{module_path}.{func_name} not found"
        assert callable(main_func), f"{module_path}.{func_name} is not callable"


class TestCLIEntryPointHelp:
    """Verify each CLI entry point responds to --help with exit code 0."""

    @pytest.mark.parametrize(
        "module_path",
        [ep[0] for ep in ENTRY_POINTS],
        ids=[
            "hephaestus-changelog",
            "hephaestus-merge-prs",
            "hephaestus-system-info",
            "hephaestus-download-dataset",
        ],
    )
    def test_help_exits_cleanly(self, module_path: str) -> None:
        """Running the entry point with --help must exit with code 0."""
        result = subprocess.run(
            [sys.executable, "-m", module_path, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"{module_path} --help exited with code {result.returncode}\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
        assert "usage:" in result.stdout.lower(), f"{module_path} --help did not produce usage text"
