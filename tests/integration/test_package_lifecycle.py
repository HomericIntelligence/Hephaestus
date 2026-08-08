#!/usr/bin/env python3
"""Integration tests for installed-package lifecycle behavior."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from .artifact_support import (
    CURRENT_TEST_VERSION,
    DIST_NAME,
    PREVIOUS_TEST_VERSION,
    ControlledArtifacts,
)

pytestmark = [pytest.mark.integration, pytest.mark.artifact]

REPRESENTATIVE_ENTRY_POINTS = (
    "hephaestus-system-info",
    "hephaestus-check-python-version",
)


def _project_console_scripts() -> tuple[str, ...]:
    """Return every console-script name declared by the project metadata."""
    project_root = Path(__file__).resolve().parents[2]
    with (project_root / "pyproject.toml").open("rb") as pyproject_file:
        pyproject = tomllib.load(pyproject_file)

    scripts = pyproject["project"]["scripts"]
    assert isinstance(scripts, dict)
    script_names: list[str] = []
    for name, target in scripts.items():
        assert isinstance(name, str)
        assert isinstance(target, str)
        script_names.append(name)
    return tuple(script_names)


PROJECT_CONSOLE_SCRIPTS = _project_console_scripts()


def _new_clean_environment(root: Path, uv: str) -> Path:
    """Create an isolated Python environment and non-checkout run directory."""
    root.mkdir(parents=True)
    venv_dir = root / "venv"
    run_dir = root / "run"
    run_dir.mkdir()
    subprocess.run(
        [uv, "venv", "--python", sys.executable, str(venv_dir)],
        cwd=run_dir,
        check=True,
        capture_output=True,
    )
    return venv_dir


def _venv_python(venv_dir: Path) -> Path:
    """Return the Python executable in a virtual environment."""
    binary_dir = "Scripts" if sys.platform == "win32" else "bin"
    executable = "python.exe" if sys.platform == "win32" else "python"
    return venv_dir / binary_dir / executable


def _run_env(venv_dir: Path) -> dict[str, str]:
    """Return a subprocess environment that cannot import the checkout."""
    del venv_dir
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    return env


def _site_packages(venv_dir: Path) -> Path:
    """Return the target environment's purelib directory."""
    python = _venv_python(venv_dir)
    result = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        cwd=venv_dir.parent / "run",
        env=_run_env(venv_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip())


def _install(venv_dir: Path, artifact: Path, uv: str, *, upgrade: bool = False) -> None:
    """Install one local artifact into an isolated environment."""
    command = [uv, "pip", "install", "--python", str(_venv_python(venv_dir))]
    if upgrade:
        command.append("--upgrade")
    command.append(str(artifact))
    subprocess.run(
        command,
        cwd=venv_dir.parent / "run",
        env=_run_env(venv_dir),
        check=True,
        capture_output=True,
    )


def _assert_distribution_absent(venv_dir: Path) -> None:
    """Prove package code and distribution metadata are absent before install."""
    python = _venv_python(venv_dir)
    probe = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import importlib.util, importlib.metadata; "
                "assert importlib.util.find_spec('hephaestus') is None; "
                "assert not any(d.metadata['Name'] == "
                f"{DIST_NAME!r} for d in importlib.metadata.distributions())"
            ),
        ],
        cwd=venv_dir.parent / "run",
        env=_run_env(venv_dir),
        check=False,
        capture_output=True,
        text=True,
    )
    assert probe.returncode == 0, probe.stderr


def _assert_installed_version(venv_dir: Path, expected: str) -> None:
    """Assert the isolated environment exposes the requested distribution version."""
    python = _venv_python(venv_dir)
    probe = subprocess.run(
        [
            str(python),
            "-c",
            f"import importlib.metadata; print(importlib.metadata.version({DIST_NAME!r}))",
        ],
        cwd=venv_dir.parent / "run",
        env=_run_env(venv_dir),
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == expected


def _run_representative_entry_points(venv_dir: Path) -> None:
    """Run representative installed base-layer scripts from outside the checkout."""
    binary_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    for command in REPRESENTATIVE_ENTRY_POINTS:
        result = subprocess.run(
            [
                str(binary_dir / (f"{command}.exe" if sys.platform == "win32" else command)),
                "--help",
            ],
            cwd=venv_dir.parent / "run",
            env=_run_env(venv_dir),
            check=False,
            capture_output=True,
            text=True,
        )
        combined = result.stdout + result.stderr
        assert result.returncode == 0, f"{command} failed:\n{combined[:1000]}"
        assert "usage" in combined.lower(), f"{command} did not print usage:\n{combined[:1000]}"


def _console_script_launchers(venv_dir: Path, command: str) -> tuple[Path, ...]:
    """Return all launcher paths generated for one console script on this platform."""
    binary_dir = venv_dir / ("Scripts" if sys.platform == "win32" else "bin")
    if sys.platform == "win32":
        return (binary_dir / f"{command}.exe", binary_dir / f"{command}-script.py")
    return (binary_dir / command,)


def _assert_console_scripts_installed(venv_dir: Path) -> None:
    """Assert every console script declared by the project has its launcher files."""
    missing_launchers = [
        launcher
        for command in PROJECT_CONSOLE_SCRIPTS
        for launcher in _console_script_launchers(venv_dir, command)
        if not launcher.exists()
    ]
    assert not missing_launchers, f"missing console-script launchers: {missing_launchers}"


def _assert_only_current_dist_info_remains(venv_dir: Path) -> None:
    """Ensure upgrading removed the previous package dist-info directory."""
    site_packages = _site_packages(venv_dir)
    dist_info = sorted(site_packages.glob("homericintelligence_hephaestus-*.dist-info"))
    assert [path.name for path in dist_info] == [
        f"homericintelligence_hephaestus-{CURRENT_TEST_VERSION}.dist-info"
    ]


def _uninstall(venv_dir: Path, uv: str) -> None:
    """Uninstall the package from an isolated environment."""
    subprocess.run(
        [uv, "pip", "uninstall", "--python", str(_venv_python(venv_dir)), DIST_NAME],
        cwd=venv_dir.parent / "run",
        env=_run_env(venv_dir),
        check=True,
        capture_output=True,
    )


def _assert_console_scripts_absent(venv_dir: Path) -> None:
    """Prove uninstall removed every platform-specific console-script launcher."""
    remaining_launchers = [
        launcher
        for command in PROJECT_CONSOLE_SCRIPTS
        for launcher in _console_script_launchers(venv_dir, command)
        if launcher.exists()
    ]
    assert not remaining_launchers, f"remaining console-script launchers: {remaining_launchers}"
    assert not (_site_packages(venv_dir) / "hephaestus").exists()
    assert not list(_site_packages(venv_dir).glob("homericintelligence_hephaestus-*.dist-info"))


def test_current_wheel_clean_install_runs_representative_entry_points(
    controlled_artifacts: ControlledArtifacts,
    tmp_path: Path,
) -> None:
    """A current wheel installs cleanly and runs representative scripts."""
    installed = _new_clean_environment(tmp_path / "current-wheel", controlled_artifacts.uv)
    _assert_distribution_absent(installed)
    _install(installed, controlled_artifacts.first_wheel, controlled_artifacts.uv)
    _assert_installed_version(installed, CURRENT_TEST_VERSION)
    _assert_console_scripts_installed(installed)
    _run_representative_entry_points(installed)


def test_current_sdist_clean_install_runs_representative_entry_points(
    controlled_artifacts: ControlledArtifacts,
    tmp_path: Path,
) -> None:
    """A current sdist installs cleanly and runs representative scripts."""
    installed = _new_clean_environment(tmp_path / "current-sdist", controlled_artifacts.uv)
    _assert_distribution_absent(installed)
    _install(installed, controlled_artifacts.first_sdist, controlled_artifacts.uv)
    _assert_installed_version(installed, CURRENT_TEST_VERSION)
    _assert_console_scripts_installed(installed)
    _run_representative_entry_points(installed)


def test_wheel_upgrade_and_clean_uninstall(
    controlled_artifacts: ControlledArtifacts,
    tmp_path: Path,
) -> None:
    """A lower-version wheel upgrades and then uninstalls without residue."""
    installed = _new_clean_environment(tmp_path / "upgrade", controlled_artifacts.uv)
    _install(installed, controlled_artifacts.previous_wheel, controlled_artifacts.uv)
    _assert_installed_version(installed, PREVIOUS_TEST_VERSION)

    _install(installed, controlled_artifacts.first_wheel, controlled_artifacts.uv, upgrade=True)
    _assert_installed_version(installed, CURRENT_TEST_VERSION)
    _assert_console_scripts_installed(installed)
    _assert_only_current_dist_info_remains(installed)
    _run_representative_entry_points(installed)

    _uninstall(installed, controlled_artifacts.uv)
    _assert_distribution_absent(installed)
    _assert_console_scripts_absent(installed)


def test_missing_build_frontend_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The artifact lane must fail when its build frontend is unavailable."""
    from . import artifact_support

    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    with pytest.raises(RuntimeError, match="build"):
        artifact_support.require_artifact_tooling()


def test_missing_uv_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The artifact lane must fail when UV is unavailable."""
    from . import artifact_support

    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda _name: object(),
    )
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="uv"):
        artifact_support.require_artifact_tooling()
