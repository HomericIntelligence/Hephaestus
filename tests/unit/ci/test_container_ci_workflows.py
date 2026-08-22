"""Executable contracts for container-backed GitHub Actions steps."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]


def _workflow_step_definition(
    workflow_name: str, job_name: str, step_name: str
) -> dict[str, object]:
    """Return a named workflow step definition."""
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / workflow_name).read_text())
    job = workflow["jobs"][job_name]
    step = next(item for item in job["steps"] if item.get("name") == step_name)
    assert isinstance(step, dict)
    return step


def _workflow_step(workflow_name: str, job_name: str, step_name: str) -> str:
    """Return the shell source from a named workflow step."""
    step = _workflow_step_definition(workflow_name, job_name, step_name)
    run = step["run"]
    assert isinstance(run, str)
    return run


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _run_step(
    tmp_path: Path, source: str, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    step = tmp_path / "step.sh"
    _write_executable(step, f"#!/usr/bin/env bash\n{source}\n")
    return subprocess.run(
        ["bash", str(step)],
        cwd=REPO_ROOT,
        env=os.environ | environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_build_artifact_step_is_valid_bash() -> None:
    """The required build lane must reach its artifact validation suite."""
    step = _workflow_step(
        "_required.yml",
        "build",
        "Validate reproducible artifacts and package lifecycle",
    )

    result = subprocess.run(["bash", "-n", "-c", step], text=True, capture_output=True, check=False)

    assert result.returncode == 0, result.stderr


def test_schema_step_builds_workflow_file_array_inside_container(tmp_path: Path) -> None:
    """Schema validation must declare and consume its inputs in the container shell."""
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "podman",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'args=("$@")\n'
        "for ((index = 0; index < ${#args[@]} - 2; index++)); do\n"
        '  if [[ "${args[index]}" == bash && "${args[index + 1]}" == -c* ]]; then\n'
        '    exec bash -c "${args[index + 2]}"\n'
        "  fi\n"
        "done\n"
        "exit 64\n",
    )
    _write_executable(
        tools / "uv",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        '[[ "${UV_OFFLINE:-}" == "1" ]]\n'
        '[[ "${UV_NO_SYNC:-}" == "1" ]]\n'
        '[[ "$1" == "run" && "$2" == "check-jsonschema" ]]\n'
        'for argument in "$@"; do\n'
        '  [[ "$argument" == .github/workflows/*.yml ]] && exit 0\n'
        "done\n"
        "exit 1\n",
    )
    step = _workflow_step(
        "_required.yml", "schema-validation", "Validate GitHub workflow schemas (in container)"
    )

    container_marker = "bash -ceu '"
    host_command, container_command = step.split(container_marker, maxsplit=1)
    assert "wf_files" not in host_command
    assert "mapfile -t wf_files" in container_command
    assert '"${wf_files[@]}"' in container_command

    result = _run_step(tmp_path, step, {"PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"})

    assert result.returncode == 0, result.stderr


def test_scheduled_zizmor_step_forwards_its_token_to_the_container(tmp_path: Path) -> None:
    """The online scheduled audit must retain its GitHub token past podman."""
    tools = tmp_path / "tools"
    tools.mkdir()
    _write_executable(
        tools / "podman",
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "forwarded=false\n"
        'args=("$@")\n'
        "for ((index = 0; index < ${#args[@]} - 1; index++)); do\n"
        '  [[ "${args[index]}" == -e && "${args[index + 1]}" == GH_TOKEN ]] && forwarded=true\n'
        "done\n"
        'if "$forwarded"; then\n'
        "  exec uv run zizmor\n"
        "fi\n"
        "unset GH_TOKEN\n"
        "exec uv run zizmor\n",
    )
    _write_executable(
        tools / "uv",
        '#!/usr/bin/env bash\nset -euo pipefail\n[[ "${GH_TOKEN:-}" == test-token ]]\n',
    )
    step_definition = _workflow_step_definition(
        "security.yml",
        "workflow-scan",
        "Run zizmor (workflow SAST with online audits, in container)",
    )
    assert step_definition["env"] == {"GH_TOKEN": "${{ github.token }}"}
    step = step_definition["run"]
    assert isinstance(step, str)

    result = _run_step(
        tmp_path,
        step,
        {"GH_TOKEN": "test-token", "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr
