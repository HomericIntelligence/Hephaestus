"""Shared helpers for reproducible artifact and lifecycle integration tests."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CURRENT_TEST_VERSION = "1.0.1"
PREVIOUS_TEST_VERSION = "1.0.0"
SOURCE_DATE_EPOCH = "1735689600"
DIST_NAME = "HomericIntelligence-Hephaestus"


@dataclass(frozen=True)
class ControlledArtifacts:
    """Paths to the artifacts built under one deterministic test profile."""

    first_wheel: Path
    second_wheel: Path
    first_sdist: Path
    second_sdist: Path
    previous_wheel: Path
    uv: str


def require_artifact_tooling() -> str:
    """Return the required UV executable or fail closed when tooling is absent."""
    if importlib.util.find_spec("build") is None:
        raise RuntimeError("required artifact frontend `python -m build` is unavailable")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("required artifact tool `uv` is unavailable")
    return uv


def file_sha256(path: Path) -> str:
    """Return the SHA-256 hexadecimal digest of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_package_files() -> set[str]:
    """Return the expected package-relative source inventory for artifacts."""
    files = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "hephaestus").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    }
    files.add("hephaestus/_version.py")
    return files


def _single(outdir: Path, pattern: str) -> Path:
    """Return the only matching artifact in an output directory."""
    matches = sorted(outdir.glob(pattern))
    if len(matches) != 1:
        raise AssertionError(f"expected one {pattern} artifact, got {matches}")
    return matches[0]


def _build(outdir: Path, version: str, *, include_sdist: bool) -> tuple[Path, Path | None]:
    """Build a wheel and optionally an sdist under a controlled environment."""
    outdir.mkdir(parents=True)
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.update(
        {
            "LC_ALL": "C.UTF-8",
            "PYTHONHASHSEED": "0",
            "SETUPTOOLS_SCM_PRETEND_VERSION": version,
            "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
            "TZ": "UTC",
        }
    )
    command = [
        sys.executable,
        "-m",
        "build",
        str(REPO_ROOT),
        "--wheel",
        "--outdir",
        str(outdir),
        "--no-isolation",
    ]
    if include_sdist:
        command.append("--sdist")
    subprocess.run(command, cwd=REPO_ROOT.parent, env=env, check=True, capture_output=True)
    return _single(outdir, "*.whl"), _single(outdir, "*.tar.gz") if include_sdist else None


def build_controlled_artifacts(artifact_root: Path) -> ControlledArtifacts:
    """Build current reproducibility pairs and a lower-version upgrade wheel."""
    uv = require_artifact_tooling()
    first_wheel, first_sdist = _build(
        artifact_root / "current-first", CURRENT_TEST_VERSION, include_sdist=True
    )
    second_wheel, second_sdist = _build(
        artifact_root / "current-second", CURRENT_TEST_VERSION, include_sdist=True
    )
    previous_wheel, previous_sdist = _build(
        artifact_root / "previous", PREVIOUS_TEST_VERSION, include_sdist=False
    )
    assert first_sdist is not None
    assert second_sdist is not None
    assert previous_sdist is None
    return ControlledArtifacts(
        first_wheel=first_wheel,
        second_wheel=second_wheel,
        first_sdist=first_sdist,
        second_sdist=second_sdist,
        previous_wheel=previous_wheel,
        uv=uv,
    )
