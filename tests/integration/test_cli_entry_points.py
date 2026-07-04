#!/usr/bin/env python3
"""Integration tests verifying every CLI entry point is importable and runnable.

The list of entry points is parsed from ``pyproject.toml`` at collection time
so adding a new ``[project.scripts]`` entry automatically gets covered with
no test edits required.

Each entry point must:

1. Have an importable target callable matching ``module:function``.
2. Respond to ``--help`` with exit code 0 when invoked as a console script.

The console-script invocation (rather than ``python -m``) is intentional —
it mirrors how end-users and other repos run these binaries, and it avoids
the CWD-shadowing class of bug that previously broke the AchaeanFleet
automation loop.
"""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover — only on Python 3.10
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _console_script_env() -> dict[str, str]:
    """Return an env that lets console scripts import the editable worktree."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _load_entry_points() -> list[tuple[str, str, str]]:
    """Return ``[(command, module, attr), ...]`` from pyproject [project.scripts]."""
    pyproject = REPO_ROOT / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    scripts: dict[str, str] = data.get("project", {}).get("scripts", {})
    parsed: list[tuple[str, str, str]] = []
    for command, target in sorted(scripts.items()):
        module, _, attr = target.partition(":")
        assert module and attr, f"Malformed entry point: {command} = {target!r}"
        parsed.append((command, module, attr))
    return parsed


ENTRY_POINTS = _load_entry_points()
ENTRY_POINT_IDS = [ep[0] for ep in ENTRY_POINTS]


class TestCLITargetImportable:
    """Every entry point's target ``module:attr`` must be an importable callable."""

    @pytest.mark.parametrize("command,module_path,attr", ENTRY_POINTS, ids=ENTRY_POINT_IDS)
    def test_target_importable(self, command: str, module_path: str, attr: str) -> None:
        # Several automation CLIs transitively import hephaestus.automation.curses_ui,
        # which depends on the stdlib `curses` module. CPython does not ship curses on
        # Windows, so these imports raise ModuleNotFoundError there. The CLIs are not
        # intended for Windows operators; skip the parametrize entry on that platform.
        if sys.platform == "win32" and "automation" in module_path:
            pytest.skip("automation CLIs require curses (not bundled on Windows)")
        mod = importlib.import_module(module_path)
        assert hasattr(mod, attr), f"{module_path} has no '{attr}' attribute"
        assert callable(getattr(mod, attr)), f"{module_path}.{attr} is not callable"


class TestCLIHelpFlag:
    """Every console script must respond to ``--help`` with exit code 0."""

    @pytest.mark.parametrize("command,module_path,attr", ENTRY_POINTS, ids=ENTRY_POINT_IDS)
    def test_help_flag(self, command: str, module_path: str, attr: str) -> None:
        # Automation CLIs transitively import POSIX-only stdlib modules
        # (`curses` for the UI, `fcntl` for cross-process locking in planner).
        # CPython on Windows ships neither; the CLIs aren't intended for
        # Windows operators. Skip the help-flag check on that platform.
        if sys.platform == "win32" and "automation" in module_path:
            pytest.skip("automation CLIs require POSIX stdlib (curses/fcntl)")
        binary: str | None = shutil.which(command)
        if binary is None:
            pytest.skip(f"{command} not on PATH — install with `pip install -e .` or run via pixi")
        assert binary is not None  # narrow for mypy; pytest.skip already returned

        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_console_script_env(),
        )
        assert result.returncode == 0, (
            f"{command} --help exited {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )
        combined = (result.stdout + result.stderr).lower()
        assert "usage" in combined, f"{command} --help did not print usage information"


class TestCLIJsonFlag:
    """Every console script must accept ``--json`` for machine-readable output."""

    @pytest.mark.parametrize("command,module_path,attr", ENTRY_POINTS, ids=ENTRY_POINT_IDS)
    def test_json_flag_documented_in_help(self, command: str, module_path: str, attr: str) -> None:
        """``<cmd> --help`` must mention ``--json`` so it is discoverable.

        We do not invoke ``<cmd> --json`` directly because most CLIs need
        additional args (file paths, repo names, gh auth, etc.) and would
        legitimately fail to produce useful output. Verifying that the flag
        appears in ``--help`` text proves the parser registered it without
        having to execute the CLI's main logic.
        """
        if sys.platform == "win32" and "automation" in module_path:
            pytest.skip("automation CLIs require POSIX stdlib (curses/fcntl)")
        binary: str | None = shutil.which(command)
        if binary is None:
            pytest.skip(f"{command} not on PATH — install with `pip install -e .` or run via pixi")
        assert binary is not None

        result = subprocess.run(
            [binary, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_console_script_env(),
        )
        assert result.returncode == 0, (
            f"{command} --help exited {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )
        combined = result.stdout + result.stderr
        assert "--json" in combined, (
            f"{command} does not advertise --json in its --help output.\n"
            "Every hephaestus-* console script must call "
            "`hephaestus.cli.utils.add_json_arg(parser)`.\n"
            f"--help output (first 800 chars):\n{combined[:800]}"
        )


class TestCLIVersionFlag:
    """Every console script must respond to ``--version`` with exit code 0."""

    @pytest.mark.parametrize("command,module_path,attr", ENTRY_POINTS, ids=ENTRY_POINT_IDS)
    def test_version_flag(self, command: str, module_path: str, attr: str) -> None:
        """``<cmd> --version`` must exit 0 and print a version line."""
        if sys.platform == "win32" and "automation" in module_path:
            pytest.skip("automation CLIs require POSIX stdlib (curses/fcntl)")
        binary: str | None = shutil.which(command)
        if binary is None:
            pytest.skip(f"{command} not on PATH — install with `pip install -e .` or run via pixi")
        assert binary is not None

        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_console_script_env(),
        )
        assert result.returncode == 0, (
            f"{command} --version exited {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )
        combined = result.stdout + result.stderr
        assert command in combined, (
            f"{command} --version output did not include the command name.\n"
            f"output: {combined[:500]}"
        )

    @pytest.mark.parametrize("command,module_path,attr", ENTRY_POINTS, ids=ENTRY_POINT_IDS)
    def test_version_flag_short_form(self, command: str, module_path: str, attr: str) -> None:
        """``<cmd> -V`` must also work (short form of --version)."""
        if sys.platform == "win32" and "automation" in module_path:
            pytest.skip("automation CLIs require POSIX stdlib (curses/fcntl)")
        binary: str | None = shutil.which(command)
        if binary is None:
            pytest.skip(f"{command} not on PATH — install with `pip install -e .` or run via pixi")
        assert binary is not None

        result = subprocess.run(
            [binary, "-V"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_console_script_env(),
        )
        assert result.returncode == 0, (
            f"{command} -V exited {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )


class TestCLIEntryPointDiscovery:
    """Sanity-check the discovery itself."""

    def test_at_least_one_entry_point(self) -> None:
        assert ENTRY_POINTS, "No entry points discovered from pyproject.toml"

    def test_no_duplicate_commands(self) -> None:
        commands = [ep[0] for ep in ENTRY_POINTS]
        assert len(commands) == len(set(commands)), "Duplicate command names in [project.scripts]"


class TestMaxWorkersValidation:
    """Regression for #723: --max-workers validation must be consistent."""

    def test_automation_loop_rejects_zero_max_workers(self) -> None:
        """hephaestus-automation-loop --max-workers=0 exits non-zero with clear error."""
        binary = shutil.which("hephaestus-automation-loop")
        if binary is None:
            pytest.skip("hephaestus-automation-loop not on PATH")
        assert binary is not None  # narrow Optional[str] for mypy

        result = subprocess.run(
            [binary, "--max-workers", "0"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=_console_script_env(),
        )
        assert result.returncode != 0, "Expected non-zero exit for --max-workers=0"
        assert "--max-workers" in result.stderr, "Error must mention --max-workers argument"
        assert "invalid choice" in result.stderr.lower(), "Error must mention invalid choice"
