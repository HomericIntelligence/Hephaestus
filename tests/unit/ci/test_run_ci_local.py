"""Behavioral contracts for the local containerized CI runner."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "scripts" / "run_ci_local.sh"


def _fake_engine(tmp_path: Path, *, failing_command: str = "") -> tuple[Path, Path]:
    """Create a controlled container-engine boundary that records invocations."""
    engine = tmp_path / "podman"
    log = tmp_path / "engine.log"
    failure_clause = (
        f'  [[ "$*" == *{failing_command!r}* ]] && exit 37\n' if failing_command else ""
    )
    engine.write_text(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [[ "$1" == "image" && "$2" == "exists" ]]; then exit 0; fi\n'
            'if [[ "$1" == "images" ]]; then exit 0; fi\n'
            'if [[ "$1" == "run" ]]; then\n'
            '  printf "%q " "$@" >> "$FAKE_ENGINE_LOG"\n'
            '  printf "\\n" >> "$FAKE_ENGINE_LOG"\n' + failure_clause + "fi\n" + "exit 0\n"
        ),
        encoding="utf-8",
    )
    engine.chmod(0o755)
    for command in ("just", "shellcheck", "bats"):
        executable = tmp_path / command
        executable.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'printf "%s " "$(basename "$0")" "$@" >> "$FAKE_ENGINE_LOG"\n'
            'printf "\\n" >> "$FAKE_ENGINE_LOG"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    return engine, log


def _run_runner(
    tmp_path: Path, subset: str, *, engine_name: str = "podman", failing_command: str = ""
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real wrapper with a deterministic successful or failing engine."""
    engine, log = _fake_engine(tmp_path, failing_command=failing_command)
    if engine_name != "podman":
        docker = engine.with_name(engine_name)
        engine.rename(docker)
        engine = docker
    environment = os.environ | {
        "CONTAINER_ENGINE": engine_name,
        "FAKE_ENGINE_LOG": str(log),
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["bash", str(RUNNER), subset],
        cwd=REPO_ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log.read_text(encoding="utf-8")


def test_lint_failure_makes_the_runner_fail(tmp_path: Path) -> None:
    """A failing first command in a multi-command check must not be masked."""
    result, _log = _run_runner(tmp_path, "lint", failing_command="uv run pre-commit")

    assert result.returncode != 0
    assert "Failed: lint" in result.stderr


def test_all_runs_every_local_required_gate(tmp_path: Path) -> None:
    """The advertised all target invokes every required local check."""
    result, log = _run_runner(tmp_path, "all")

    assert result.returncode == 0, result.stderr
    for command in (
        "bash scripts/check-symlinks.sh",
        "just --evaluate",
        "shellcheck --severity=error",
        "bats --recursive tests/shell",
        "detect --source=. --verbose --exit-code=1",
        "HEPHAESTUS_REQUIRE_CLI=1",
    ):
        assert command in log


def test_docker_uses_the_invoking_user_for_writable_mounts(tmp_path: Path) -> None:
    """Docker fallback must not run bind-mounted checks as image-owned UID 1000."""
    result, log = _run_runner(tmp_path, "unit", engine_name="docker")

    assert result.returncode == 0, result.stderr
    assert f"--user {os.getuid()}:{os.getgid()}" in log
