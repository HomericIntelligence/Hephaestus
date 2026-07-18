"""Integration coverage for the mandatory generic Gitleaks hook."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GITLEAKS_REPO = "https://github.com/gitleaks/gitleaks"
GITLEAKS_VERSION = "v8.30.0"
_GIT_REPO_ENV_KEYS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)

pytestmark = pytest.mark.integration


def _isolated_env(cache: Path) -> dict[str, str]:
    """Return an environment that isolates Git and pre-commit state."""
    env = {
        **os.environ,
        "PRE_COMMIT_HOME": str(cache),
        "PRE_COMMIT_COLOR": "never",
        # The Gitleaks hook builds with Go. Keep its build cache inside this
        # test's temporary home instead of inheriting an operator-owned cache
        # that may not be writable in a sandboxed test process.
        "GOCACHE": str(cache / "go-build"),
    }
    for key in _GIT_REPO_ENV_KEYS:
        env.pop(key, None)
    return env


def test_gitleaks_hook_is_mandatory_and_matches_ci_version() -> None:
    """The local generic scanner must be pre-commit scoped and CI-aligned."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    repo = next(item for item in config["repos"] if item.get("repo") == GITLEAKS_REPO)
    hook = next(item for item in repo["hooks"] if item.get("id") == "gitleaks")

    assert repo["rev"] == GITLEAKS_VERSION
    assert hook["stages"] == ["pre-commit"]
    workflow = (REPO_ROOT / ".github/workflows/_required.yml").read_text(encoding="utf-8")
    assert f"gitleaks:{GITLEAKS_VERSION}@sha256:" in workflow


@pytest.mark.requires_posix
def test_gitleaks_blocks_staged_secret_without_private_denylist(tmp_path: Path) -> None:
    """Gitleaks must reject staged credentials without an operator denylist."""
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy2(
        REPO_ROOT / ".pre-commit-config.yaml",
        repo / ".pre-commit-config.yaml",
    )
    env = _isolated_env(tmp_path / "pre-commit-cache")
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)

    assert not (repo / ".heph-private-denylist").exists()
    fake_secret = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
    (repo / "credentials.txt").write_text(
        f'api_token = "{fake_secret}"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "credentials.txt"], cwd=repo, env=env, check=True)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pre_commit",
            "run",
            "gitleaks",
            "--config",
            ".pre-commit-config.yaml",
            "--files",
            "credentials.txt",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    output = f"{result.stdout}\n{result.stderr}"

    assert result.returncode == 1, output
    assert "Gitleaks (generic staged-secret scan)" in output
    assert "Failed" in output
    assert fake_secret not in output
