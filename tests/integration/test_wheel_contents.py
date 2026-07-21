#!/usr/bin/env python3
"""Integration test: built wheel ships correct contents and installs cleanly."""

from __future__ import annotations

import configparser
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]

EXPECTED_PACKAGE_FILES = {
    "hephaestus/__init__.py",
    # Generated at build time by the hatch-vcs build hook; never committed.
    "hephaestus/_version.py",
    "hephaestus/automation/__init__.py",
    "hephaestus/utils/__init__.py",
}

FORBIDDEN_PREFIXES = ("tests/", "scripts/", "docs/", ".github/", ".claude/")


@pytest.fixture(scope="module")
def wheel_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel once for all tests in this module."""
    probe = subprocess.run(
        [sys.executable, "-m", "build", "--help"],
        cwd=REPO_ROOT.parent,
        check=False,
        capture_output=True,
    )
    if probe.returncode != 0 and b"No module named build" in probe.stderr:
        pytest.skip("python build frontend is not installed in this environment")

    outdir = tmp_path_factory.mktemp("wheel")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            str(REPO_ROOT),
            "--wheel",
            "--outdir",
            str(outdir),
            # Contents, not isolation, are under test; the UV env already
            # provides the backend (see test_sdist_contents.py).
            "--no-isolation",
        ],
        cwd=REPO_ROOT.parent,
        check=True,
        capture_output=True,
    )
    wheels = list(outdir.glob("homericintelligence_hephaestus-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, got {wheels}"
    return wheels[0]


def _members(wheel: Path) -> list[str]:
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


def _dist_info_prefix(members: list[str]) -> str:
    prefixes = {m.split("/", 1)[0] for m in members if m.split("/", 1)[0].endswith(".dist-info")}
    assert len(prefixes) == 1, f"expected one dist-info dir, got {prefixes}"
    return prefixes.pop()


@pytest.mark.integration
def test_wheel_contains_package_and_metadata(wheel_path: Path) -> None:
    """The wheel ships the package, the vcs version file, and dist-info metadata."""
    members = _members(wheel_path)
    missing = EXPECTED_PACKAGE_FILES - set(members)
    assert not missing, f"wheel is missing expected files: {sorted(missing)}"

    dist_info = _dist_info_prefix(members)
    for meta in ("METADATA", "entry_points.txt", "licenses/LICENSE"):
        assert f"{dist_info}/{meta}" in members, f"wheel is missing {meta}"


@pytest.mark.integration
def test_wheel_excludes_repo_scaffolding(wheel_path: Path) -> None:
    """Only the hephaestus package ships — no tests, scripts, docs, or CI config."""
    offenders = [m for m in _members(wheel_path) if m.startswith(FORBIDDEN_PREFIXES)]
    assert not offenders, f"wheel leaked repo scaffolding: {offenders}"


@pytest.mark.integration
def test_wheel_console_scripts_match_pyproject(wheel_path: Path) -> None:
    """entry_points.txt console_scripts mirror [project.scripts] exactly."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = pyproject["project"]["scripts"]

    members = _members(wheel_path)
    dist_info = _dist_info_prefix(members)
    with zipfile.ZipFile(wheel_path) as zf:
        raw = zf.read(f"{dist_info}/entry_points.txt").decode("utf-8")
    parser = configparser.ConfigParser()
    parser.read_string(raw)
    actual = dict(parser["console_scripts"])

    assert actual == expected


@pytest.mark.integration
def test_wheel_installs_and_imports_in_fresh_venv(wheel_path: Path, tmp_path: Path) -> None:
    """Installing the wheel in a clean venv yields a working base import (CI smoke parity)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not installed in this environment")

    venv_dir = tmp_path / "venv"
    subprocess.run([uv, "venv", str(venv_dir)], check=True, capture_output=True)
    bin_dir = "Scripts" if sys.platform == "win32" else "bin"
    venv_python = venv_dir / bin_dir / ("python.exe" if sys.platform == "win32" else "python")

    subprocess.run(
        [uv, "pip", "install", "--python", str(venv_python), str(wheel_path)],
        check=True,
        capture_output=True,
    )

    probe_code = "; ".join(
        [
            "import sys",
            "import hephaestus",
            "from hephaestus import slugify, retry_with_backoff, setup_logging",
            "assert 'pydantic' not in sys.modules, 'base import pulled pydantic'",
            "assert 'hephaestus.automation' not in sys.modules, 'base import pulled automation'",
            "print(hephaestus.__version__)",
        ]
    )
    probe = subprocess.run(
        [str(venv_python), "-c", probe_code],
        check=True,
        capture_output=True,
        text=True,
    )
    installed_version = probe.stdout.strip()
    assert installed_version
    # The wheel filename embeds the version hatch-vcs computed at build time.
    assert wheel_path.name.split("-")[1] == installed_version
