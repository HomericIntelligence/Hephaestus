"""Real-container coverage for the local CI Docker permission boundary."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_IMAGE = "hephaestus-ci:local"
DOCKER = shutil.which("docker")


def _local_ci_image_is_available() -> bool:
    """Return whether a usable Docker daemon has the locally built CI image."""
    if DOCKER is None:
        return False
    result = subprocess.run(
        [DOCKER, "image", "inspect", CI_IMAGE],
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result.returncode == 0


@pytest.mark.integration
@pytest.mark.skipif(
    not _local_ci_image_is_available(),
    reason="requires Docker with a locally built hephaestus-ci:local image",
)
def test_arbitrary_docker_uid_can_sync_uv_and_write_bind_artifact(tmp_path: Path) -> None:
    """An arbitrary host UID can refresh uv and create a bind-mounted artifact."""
    assert DOCKER is not None
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(mode=0o777)
    artifact_dir.chmod(0o777)

    result = subprocess.run(
        [
            DOCKER,
            "run",
            "--rm",
            "--user",
            "23456:23457",
            "--env",
            "HOME=/tmp",
            "--env",
            "UV_PROJECT_ENVIRONMENT=/opt/hephaestus-venv",
            "--tmpfs",
            "/tmp:rw,size=4g,mode=1777",
            "--volume",
            f"{REPO_ROOT}:/workspace",
            "--volume",
            f"{artifact_dir}:/artifacts",
            "--workdir",
            "/workspace",
            CI_IMAGE,
            "bash",
            "-ceu",
            "uv run --locked python -c 'import hephaestus' && touch /artifacts/created",
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert (artifact_dir / "created").is_file()
