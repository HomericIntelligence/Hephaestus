"""Behavioral contracts for the local containerized CI runner."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER = REPO_ROOT / "scripts" / "run_ci_local.sh"
FAKE_IMAGE_ID = f"sha256:{'a' * 64}"
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _fake_engine(
    tmp_path: Path,
    *,
    failing_command: str = "",
    license_violation: bool = False,
    image_exists: bool = True,
    image_id: str = FAKE_IMAGE_ID,
    external_git_common_dir: Path | None = None,
) -> tuple[Path, Path]:
    """Create a controlled container-engine boundary that records invocations."""
    engine_path = tmp_path / "podman"
    log = tmp_path / "engine.log"
    failure_clause = (
        f'  [[ "$*" == *{failing_command!r}* ]] && exit 37\n' if failing_command else ""
    )
    license_violation_clause = (
        '  [[ "$FAKE_LICENSE_VIOLATION" == "1" && "$*" == *'
        '"env GITHUB_EVENT_NAME=pull_request uv run python '
        'scripts/check_license_compatibility.py"* ]] && exit 1\n'
        if license_violation
        else ""
    )
    engine_path.write_text(
        (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'if [[ "$1" == "image" && "$2" == "exists" ]]; then '
            f"exit {0 if image_exists else 1}; fi\n"
            'if [[ "$1" == "image" && "$2" == "inspect" ]]; then '
            f'printf "%s\\n" "{image_id}"; exit 0; fi\n'
            'if [[ "$1" == "image" && "$2" == "rm" ]]; then exit 0; fi\n'
            'if [[ "$1" == "images" ]]; then exit 0; fi\n'
            'if [[ "$1" == "build" ]]; then\n'
            '  printf "%q " "$@" >> "$FAKE_ENGINE_LOG"\n'
            '  printf "\\n" >> "$FAKE_ENGINE_LOG"\n'
            '  previous=""\n'
            '  for arg in "$@"; do\n'
            '    if [[ "$previous" == "--iidfile" ]]; then\n'
            f'      printf "%s\\n" "{image_id}" > "$arg"\n'
            "    fi\n"
            '    previous="$arg"\n'
            "  done\n"
            '  find . -type f -print | sed "s|^|BUILD_CONTEXT_FILE:|" '
            '>> "$FAKE_ENGINE_LOG"\n'
            "fi\n"
            'if [[ "$1" == "run" ]]; then\n'
            '  printf "%q " "$@" >> "$FAKE_ENGINE_LOG"\n'
            '  printf "\\n" >> "$FAKE_ENGINE_LOG"\n'
            '  workspace_root=""\n'
            '  candidate_root=""\n'
            '  candidate_index=""\n'
            '  candidate_objects=""\n'
            '  for arg in "$@"; do\n'
            '    case "$arg" in\n'
            "      *:/workspace:Z)\n"
            '        workspace_root="${arg%:/workspace:Z}"\n'
            "        ;;\n"
            "      *:/candidate:ro)\n"
            '        candidate_root="${arg%:/candidate:ro}"\n'
            "        ;;\n"
            "      GIT_INDEX_FILE=/workspace/*)\n"
            '        candidate_index="${arg#GIT_INDEX_FILE=/workspace/}"\n'
            "        ;;\n"
            "      GIT_ALTERNATE_OBJECT_DIRECTORIES=/workspace/*)\n"
            '        candidate_objects="${arg#GIT_ALTERNATE_OBJECT_DIRECTORIES=/workspace/}"\n'
            "        ;;\n"
            "    esac\n"
            "  done\n"
            '  if [[ -n "$candidate_index" ]] && '
            'GIT_INDEX_FILE="$workspace_root/$candidate_index" '
            'GIT_ALTERNATE_OBJECT_DIRECTORIES="$workspace_root/$candidate_objects" '
            '/usr/bin/git -C "$workspace_root" cat-file -e :new_source.py; then\n'
            '    printf "CANDIDATE_INDEX_BYTES:" >> "$FAKE_ENGINE_LOG"\n'
            '    GIT_INDEX_FILE="$workspace_root/$candidate_index" '
            'GIT_ALTERNATE_OBJECT_DIRECTORIES="$workspace_root/$candidate_objects" '
            '/usr/bin/git -C "$workspace_root" show :new_source.py '
            '>> "$FAKE_ENGINE_LOG"\n'
            "  fi\n"
            '  if [[ "$*" == *"gitleaks"* && -n "$candidate_root" && '
            '-f "$candidate_root/new_secret_source.txt" ]]; then\n'
            '    printf "CANDIDATE_SECRET_BYTES:" >> "$FAKE_ENGINE_LOG"\n'
            '    cat "$candidate_root/new_secret_source.txt" >> "$FAKE_ENGINE_LOG"\n'
            '    if grep -q "fixture-secret-value" '
            '"$candidate_root/new_secret_source.txt"; then exit 42; fi\n'
            "  fi\n" + failure_clause + license_violation_clause + "fi\n" + "exit 0\n"
        ),
        encoding="utf-8",
    )
    engine_path.chmod(0o755)
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
    if external_git_common_dir is not None:
        git = tmp_path / "git"
        git.write_text(
            (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'printf "%s\\n" "{external_git_common_dir}"\n'
            ),
            encoding="utf-8",
        )
        git.chmod(0o755)
    return engine_path, log


def _run_runner(
    tmp_path: Path,
    subset: str,
    *,
    engine_name: str = "podman",
    failing_command: str = "",
    license_violation: bool = False,
    host_uid: int | None = None,
    host_gid: int | None = None,
    image_exists: bool = True,
    image_id: str = FAKE_IMAGE_ID,
    rebuild_image: bool = False,
    external_git_common_dir: Path | None = None,
    repo_root: Path = REPO_ROOT,
    color_environment: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run the real wrapper with a deterministic successful or failing engine."""
    engine_path, log = _fake_engine(
        tmp_path,
        failing_command=failing_command,
        license_violation=license_violation,
        image_exists=image_exists,
        image_id=image_id,
        external_git_common_dir=external_git_common_dir,
    )
    if engine_name != "podman":
        docker = engine_path.with_name(engine_name)
        engine_path.rename(docker)
    if host_uid is not None and host_gid is not None:
        fake_id = tmp_path / "id"
        fake_id.write_text(
            (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                f'[[ "$1" == "-u" ]] && printf "%s\\n" "{host_uid}" && exit 0\n'
                f'[[ "$1" == "-g" ]] && printf "%s\\n" "{host_gid}" && exit 0\n'
                'printf "unsupported id argument: %s\\n" "$1" >&2\n'
                "exit 2\n"
            ),
            encoding="utf-8",
        )
        fake_id.chmod(0o755)
    environment = os.environ | {
        "CONTAINER_ENGINE": engine_name,
        "FAKE_ENGINE_LOG": str(log),
        "FAKE_LICENSE_VIOLATION": "1" if license_violation else "0",
        "HEPHAESTUS_CI_REBUILD": "1" if rebuild_image else "0",
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
    }
    for name in ("NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE"):
        environment.pop(name, None)
    if color_environment:
        environment.update(color_environment)
    result = subprocess.run(
        ["bash", str(repo_root / "scripts" / "run_ci_local.sh"), subset],
        cwd=repo_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log.read_text(encoding="utf-8") if log.exists() else ""


def _candidate_repo(tmp_path: Path) -> Path:
    """Create a tiny repository that executes the real local-CI wrapper."""
    repo = tmp_path / "candidate-repo"
    (repo / "scripts" / "shell" / "lib").mkdir(parents=True)
    shutil.copy2(RUNNER, repo / "scripts" / "run_ci_local.sh")
    shutil.copy2(
        REPO_ROOT / "scripts" / "shell" / "lib" / "install_helpers.sh",
        repo / "scripts" / "shell" / "lib" / "install_helpers.sh",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.py").write_text("TRACKED = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=CI Test",
            "-c",
            "user.email=ci@example.invalid",
            "commit",
            "--no-gpg-sign",
            "-q",
            "-m",
            "test: seed candidate repo",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def _buildable_candidate_repo(tmp_path: Path) -> Path:
    """Create the minimal publishable source set consumed by the image build."""
    repo = _candidate_repo(tmp_path)
    (repo / "ci").mkdir()
    (repo / "hephaestus").mkdir()
    for relative_path in (
        "ci/Containerfile",
        "uv.lock",
        "pyproject.toml",
        ".pre-commit-config.yaml",
        "README.md",
        "hephaestus/module.py",
    ):
        (repo / relative_path).write_text(f"fixture: {relative_path}\n", encoding="utf-8")
    (repo / ".gitignore").write_text("ignored.env\n", encoding="utf-8")
    (repo / "ignored.env").write_text("must not enter build context\n", encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("failing_command", "failed_step"),
    [
        ("uv run pre-commit", "lint"),
        ("hephaestus.scripts_lib.check_version_single_source", "version"),
    ],
)
def test_all_preserves_failure_from_multi_command_check(
    tmp_path: Path, failing_command: str, failed_step: str
) -> None:
    """The all target must aggregate an inner failure and continue later gates."""
    result, log = _run_runner(tmp_path, "all", failing_command=failing_command)

    assert result.returncode != 0
    assert f"Failed: {failed_step}" in result.stderr
    assert "detect --source=. --verbose --exit-code=1" in log


def test_all_runs_every_local_required_gate(tmp_path: Path) -> None:
    """The advertised all target invokes every required local check."""
    result, log = _run_runner(tmp_path, "all")

    assert result.returncode == 0, result.stderr
    assert "All locally executable CI checks passed." in result.stdout
    for command in (
        "GIT_INDEX_FILE=/workspace/build/ci-candidate.",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES=/workspace/build/ci-candidate.",
        "uv run pre-commit run --all-files --show-diff-on-failure",
        "uv run hephaestus-validate-links docs --repo-root .",
        "uv run pytest tests/unit",
        "uv run hephaestus-check-test-structure",
        "uv run hephaestus-check-coverage --coverage-file coverage.xml --config coverage.toml",
        "HEPHAESTUS_REQUIRE_CLI=1 uv run pytest tests/integration",
        "uv build --wheel",
        "HEPHAESTUS_REQUIRE_CLI=1 build/cli-venv/bin/pytest",
        "uv run pytest tests/integration --override-ini=addopts= "
        "--basetemp=build/pytest-artifacts -v --strict-markers -m artifact",
        "uv run pip-audit",
        "uv run bandit -c pyproject.toml -r hephaestus scripts --severity-level medium",
        "uv run zizmor --no-online-audits --min-severity medium .github/workflows/",
        "uv run check-jsonschema --builtin-schema vendor.github-workflows",
        "hephaestus.scripts_lib.check_version_single_source",
        "uv lock --check",
        "bash scripts/check-symlinks.sh",
        "just --evaluate",
        "shellcheck --severity=error",
        "bats --recursive tests/shell",
        "detect --source=. --verbose --exit-code=1",
        "dir --verbose --exit-code=1 .",
        "HEPHAESTUS_REQUIRE_CLI=1",
        "env GITHUB_EVENT_NAME=pull_request uv run python scripts/check_license_compatibility.py",
    ):
        assert command in log


def test_lint_candidate_index_includes_untracked_source(tmp_path: Path) -> None:
    """New source files are visible to every pre-commit hook before publication."""
    repo = _candidate_repo(tmp_path)
    candidate_bytes = "NEW = True\n"
    (repo / "new_source.py").write_text(candidate_bytes, encoding="utf-8")
    candidate_blob = subprocess.run(
        ["git", "hash-object", "--stdin"],
        cwd=repo,
        input=candidate_bytes,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert (
        subprocess.run(["git", "cat-file", "-e", candidate_blob], cwd=repo, check=False).returncode
        != 0
    )

    before_index = subprocess.run(
        ["git", "write-tree"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout
    result, log = _run_runner(tmp_path, "lint", repo_root=repo)

    assert result.returncode == 0, result.stderr
    assert "CANDIDATE_INDEX_BYTES:NEW = True" in log
    assert "GIT_INDEX_FILE=/workspace/build/ci-candidate." in log
    after_index = subprocess.run(
        ["git", "write-tree"], cwd=repo, text=True, capture_output=True, check=True
    ).stdout
    assert after_index == before_index
    assert (
        subprocess.run(["git", "cat-file", "-e", candidate_blob], cwd=repo, check=False).returncode
        != 0
    )
    assert not list((repo / "build").glob("ci-candidate.*"))


def test_secrets_candidate_tree_includes_untracked_source(tmp_path: Path) -> None:
    """The filesystem scanner receives the exact uncommitted candidate tree."""
    repo = _candidate_repo(tmp_path)
    fixture_content = "fixture-secret-value\n"
    (repo / "new_secret_source.txt").write_text(fixture_content, encoding="utf-8")

    result, log = _run_runner(tmp_path, "secrets", repo_root=repo)

    assert result.returncode != 0
    assert f"CANDIDATE_SECRET_BYTES:{fixture_content}" in log
    assert "dir --verbose --exit-code=1 ." in log
    assert not list((repo / "build").glob("ci-candidate.*"))


def test_all_fails_for_an_injected_license_violation(tmp_path: Path) -> None:
    """The all target must preserve a blocking PR-mode license failure."""
    result, log = _run_runner(tmp_path, "all", license_violation=True)

    assert result.returncode != 0
    assert "Failed: license" in result.stderr
    assert (
        "env GITHUB_EVENT_NAME=pull_request uv run python "
        "scripts/check_license_compatibility.py" in log
    )


def test_explicit_license_mode_remains_advisory(tmp_path: Path) -> None:
    """The standalone license subset retains the scanner's normal invocation."""
    result, log = _run_runner(tmp_path, "license")

    assert result.returncode == 0, result.stderr
    assert "uv run python scripts/check_license_compatibility.py" in log
    assert "GITHUB_EVENT_NAME=pull_request" not in log


def test_integration_requires_installed_cli_entry_points(tmp_path: Path) -> None:
    """The integration lane must fail rather than skip when a CLI is absent."""
    result, log = _run_runner(tmp_path, "integration")

    assert result.returncode == 0, result.stderr
    assert "HEPHAESTUS_REQUIRE_CLI=1 uv run pytest tests/integration" in log


def test_build_matches_required_artifact_lane(tmp_path: Path) -> None:
    """The local build subset must execute the required workflow's artifact gate."""
    result, log = _run_runner(tmp_path, "build")

    assert result.returncode == 0, result.stderr
    assert (
        "uv run pytest tests/integration --override-ini=addopts= "
        "--basetemp=build/pytest-artifacts -v --strict-markers -m artifact"
    ) in log
    assert "python -m build --no-isolation" not in log


def test_all_separates_general_integration_from_artifact_lane(tmp_path: Path) -> None:
    """The serialized local gate must not execute artifact tests twice."""
    result, log = _run_runner(tmp_path, "all")

    assert result.returncode == 0, result.stderr
    assert '-m "not nightly and not artifact"' in log
    assert log.count("-m artifact") == 1


def test_missing_ci_image_is_built_automatically(tmp_path: Path) -> None:
    """The autonomous queue must not require a manual ``just ci-build`` step."""
    result, log = _run_runner(tmp_path, "unit", image_exists=False)

    assert result.returncode == 0, result.stderr
    assert "build --iidfile" in log
    assert "-t hephaestus-ci:run-" in log
    assert "-t hephaestus-ci:local ." in log
    assert FAKE_IMAGE_ID in log
    assert "uv run pytest tests/unit" in log
    assert "BUILD_CONTEXT_FILE:./ci/Containerfile" in log
    assert "BUILD_CONTEXT_FILE:./.git/" not in log
    assert "BUILD_CONTEXT_FILE:./build/" not in log


def test_queue_mode_rebuilds_an_existing_ci_image(tmp_path: Path) -> None:
    """Queue execution must test with dependencies built from the current checkout."""
    result, log = _run_runner(tmp_path, "unit", rebuild_image=True)

    assert result.returncode == 0, result.stderr
    assert "build --iidfile" in log
    assert "-t hephaestus-ci:run-" in log
    assert "-t hephaestus-ci:local ." in log
    assert FAKE_IMAGE_ID in log
    assert "uv run pytest tests/unit" in log


def test_podman_bare_image_id_is_accepted_as_immutable(tmp_path: Path) -> None:
    """Podman may omit Docker's ``sha256:`` prefix from a full image ID."""
    podman_image_id = "b" * 64

    result, log = _run_runner(tmp_path, "unit", image_id=podman_image_id)

    assert result.returncode == 0, result.stderr
    assert f"{podman_image_id} bash" in log


def test_image_build_context_excludes_ignored_checkout_files(tmp_path: Path) -> None:
    """Only publishable allowlisted files are sent to either container engine."""
    repo = _buildable_candidate_repo(tmp_path)

    result, log = _run_runner(tmp_path, "unit", image_exists=False, repo_root=repo)

    assert result.returncode == 0, result.stderr
    assert "BUILD_CONTEXT_FILE:./hephaestus/module.py" in log
    assert "BUILD_CONTEXT_FILE:./ignored.env" not in log
    assert "BUILD_CONTEXT_FILE:./.git/" not in log


def test_image_build_rejects_allowlisted_symlink_sources(tmp_path: Path) -> None:
    """A staged symlink must not dereference ignored host bytes into the build context."""
    repo = _buildable_candidate_repo(tmp_path)
    pyproject = repo / "pyproject.toml"
    pyproject.unlink()
    pyproject.symlink_to((repo / "ignored.env").resolve())

    result, log = _run_runner(tmp_path, "unit", image_exists=False, repo_root=repo)

    assert result.returncode != 0
    assert "Candidate build source must be a regular file" in result.stderr
    assert "build --iidfile" not in log


def test_image_build_rejects_allowlisted_symlink_ancestors(tmp_path: Path) -> None:
    """An allowlisted directory cannot redirect the build recipe outside the candidate tree."""
    repo = _buildable_candidate_repo(tmp_path)
    shutil.rmtree(repo / "ci")
    external_ci = tmp_path / "ignored-ci"
    external_ci.mkdir()
    (external_ci / "Containerfile").write_text("FROM scratch\n", encoding="utf-8")
    (repo / "ci").symlink_to(external_ci.resolve(), target_is_directory=True)

    result, log = _run_runner(tmp_path, "unit", image_exists=False, repo_root=repo)

    assert result.returncode != 0
    assert "Candidate build source must be a regular file" in result.stderr
    assert "build --iidfile" not in log


def test_schema_validator_is_part_of_the_locked_dev_environment() -> None:
    """Schema validation must not download mutable executable code at gate runtime."""
    config = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = config["dependency-groups"]["dev"]

    assert any(dependency.startswith("check-jsonschema>=") for dependency in dev_dependencies)
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    assert any(package["name"] == "check-jsonschema" for package in packages)
    root = next(
        package for package in packages if package["name"] == "homericintelligence-hephaestus"
    )
    assert any(
        dependency["name"] == "check-jsonschema" for dependency in root["dev-dependencies"]["dev"]
    )
    workflow = (REPO_ROOT / ".github/workflows/_required.yml").read_text(encoding="utf-8")
    assert "uv run check-jsonschema" in workflow
    assert "uvx check-jsonschema" not in workflow


def test_linked_worktree_git_metadata_is_mounted_read_only(tmp_path: Path) -> None:
    """Container checks must resolve linked-worktree Git metadata."""
    common_dir = tmp_path / "outside" / "repo.git"
    common_dir.mkdir(parents=True)

    result, log = _run_runner(
        tmp_path,
        "unit",
        external_git_common_dir=common_dir,
    )

    assert result.returncode == 0, result.stderr
    assert f"--volume {common_dir}:{common_dir}:ro" in log


@pytest.mark.parametrize(
    ("subset", "command"),
    [
        ("justfile", f"{FAKE_IMAGE_ID} just --evaluate"),
        ("shellcheck", f"{FAKE_IMAGE_ID} shellcheck --severity=error"),
        ("shell-tests", f"{FAKE_IMAGE_ID} bats --recursive tests/shell"),
    ],
)
def test_shell_gates_run_in_ci_image(tmp_path: Path, subset: str, command: str) -> None:
    """Required shell tools must not depend on machine-local installations."""
    result, log = _run_runner(tmp_path, subset)

    assert result.returncode == 0, result.stderr
    assert command in log


def test_subset_success_message_names_only_the_requested_subset(tmp_path: Path) -> None:
    """A successful subset must not claim that every local CI check ran."""
    result, _ = _run_runner(tmp_path, "integration")

    assert result.returncode == 0, result.stderr
    assert "Local CI subset 'integration' passed." in result.stdout
    assert "All local CI checks passed." not in result.stdout


def test_non_tty_ci_runner_output_is_plain_by_default(tmp_path: Path) -> None:
    """The executable CI runner does not emit ANSI into redirected output."""
    result, _ = _run_runner(tmp_path, "integration")

    assert result.returncode == 0, result.stderr
    assert ANSI.search(result.stdout) is None


def test_no_color_wins_over_force_color_for_ci_runner(tmp_path: Path) -> None:
    """NO_COLOR suppresses ANSI even when a force control is also set."""
    result, _ = _run_runner(
        tmp_path,
        "integration",
        color_environment={"NO_COLOR": "1", "FORCE_COLOR": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert ANSI.search(result.stdout) is None


def test_force_color_enables_ansi_for_non_tty_ci_runner(tmp_path: Path) -> None:
    """FORCE_COLOR enables the shared policy when stdout is redirected."""
    result, _ = _run_runner(
        tmp_path,
        "integration",
        color_environment={"FORCE_COLOR": "1"},
    )

    assert result.returncode == 0, result.stderr
    assert ANSI.search(result.stdout) is not None


def test_docker_uses_the_invoking_user_for_writable_mounts(tmp_path: Path) -> None:
    """Docker uses its baked environment without syncing as an arbitrary UID."""
    result, log = _run_runner(
        tmp_path,
        "unit",
        engine_name="docker",
        host_uid=23456,
        host_gid=23457,
    )

    assert result.returncode == 0, result.stderr
    assert "--user 23456:23457" in log
    assert "--env HOME=/tmp" in log
    assert "--env UV_NO_SYNC=1" in log
    assert "--env PYTHONPATH=/workspace" in log
    assert "UV_PROJECT_ENVIRONMENT" not in log


def test_podman_maps_the_ci_user_to_the_invoking_user(tmp_path: Path) -> None:
    """Podman prevents Git dubious-ownership failures on the mounted checkout."""
    result, log = _run_runner(tmp_path, "shell-tests")

    assert result.returncode == 0, result.stderr
    assert "--userns=keep-id:uid=1000\\,gid=1000" in log
