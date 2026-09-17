#!/usr/bin/env python3
"""Validate deployed artifacts without publication.

The dispatcher maps each validator identifier to one fixed implementation.
OCI checks use the selected Docker or Podman engine.
Artifact fixtures require rootless Podman.
Fixtures mount the repository read-only and disable the container network.

Validators verify reviewed inputs, output contents, permissions, and cleanup.
The validator fails closed for an unknown validator or unsafe input.
It also fails closed for a failed command or invalid result.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from smg_wheelhouse import (
    DependencyLock as _DependencyLock,
)
from smg_wheelhouse import (
    load_dependency_lock,
    prepare_wheelhouse,
    verify_wheelhouse,
)

DependencyLock = _DependencyLock

# Reviewed inputs, validator registration, and bounded-data limits.
LINUX_PLATFORM = "linux/amd64"
CONTROL_PYTHON_IMAGE_REF = (
    "docker.io/library/python:3.12-slim@"
    "sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea"
)
CONTROL_UV_IMAGE_REF = (
    "ghcr.io/astral-sh/uv:0.12.7@"
    "sha256:ef1726226744409e420a0b4903a915602186c356ae626be0a673b11ac87b3860"
)
SMG_DOCKER_UBUNTU_IMAGE_REF = (
    "docker.io/library/ubuntu:26.04@"
    "sha256:513c074113a871b51a8d16ab445c88779d6452d937a164fb5cc479f32668a41d"
)
SMG_UBUNTU_ENROOT_REF = (
    "docker://registry-1.docker.io#library/ubuntu:"
    "sha256:1e0a86e57d247923571b75e0aaf48a1449cf8c543d51fb3e07a4a7d7bfa79316"
)
SMG_DOCKER_UBUNTU_SNAPSHOT = "20260825T000000Z"
SMG_ENROOT_UBUNTU_SNAPSHOT = "20260825T000000Z"
SMG_DOCKER_PACKAGE_FILE_PATTERN = re.compile(
    r"^COPY docker/(smg-apt-packages-\d+\.\d+\.txt) /tmp/smg-apt-packages-\d+\.\d+\.txt$",
    re.MULTILINE,
)
SMG_ENROOT_PACKAGE_FILE_PATTERN = re.compile(
    r"--arg-file=(?:/comet/docker|/tmp)/(smg-apt-packages-\d+\.\d+\.txt)"
)
SMG_WHEEL_NAME = "smg-1.9.0-cp38-abi3-manylinux_2_39_x86_64.whl"
SMG_WHEEL_SHA256 = "73c04e8189c5aed2c18f349935222fe955782a4e842355cefc9f5172b0a38e59"
SMG_WHEEL_URL = (
    "https://github.com/LLM360/smg/releases/download/wheel/prod-d636bcd1/" + SMG_WHEEL_NAME
)
SMG_REQUIRES_DIST = (
    "setproctitle",
    "grpcio",
    "grpcio-health-checking",
    "pyyaml",
    "mypy ; extra == 'dev'",
    "requests>=2.25.0 ; extra == 'dev'",
    "ruff ; extra == 'dev'",
    "pytest>=7.0.0 ; extra == 'dev'",
)
SMG_ROUTER_FLAGS = (
    "--policy",
    "--max-cached-owners-per-prefix",
    "--cache-owner-spill-cooldown-secs",
    "--engine-metrics",
    "--adaptive-admission-mode",
    "--adaptive-admission-strategy",
    "--prefer-trusted-tenant-header",
    "--capacity-credit-generation",
    "--priority-scheduler-adaptive-capacity",
    "--adaptive-admission-distribution-headroom-partitions",
    "--adaptive-admission-distribution-headroom-partition-seed-cap",
    "--least-load-cache-mode",
    "--least-load-default-throughput",
    "--least-load-cache-prefill-throughput",
    "--least-load-mean-remaining-decode-tokens",
)
SMG_DOCKER_FLAGS = SMG_ROUTER_FLAGS[-5:]
TOOLS_BASE_IMAGE = (
    "docker.io/library/python@"
    "sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17"
)
TOOLS_DEBIAN_SNAPSHOT = "20260825T000000Z"
TOOLS_GIT_VERSION = "1:2.47.3-0+deb13u1"
TOOLS_SQUASHFS_VERSION = "1:4.6.1-1"
REVIEWED_DEPLOYED_PROGRAM_DIGESTS = {
    "scripts/build-smg-image.sh": (
        "84134bda92f2d45fbbaf84b66c650fffbfe713ab5ca12bd59a7ef52044af259a"
    ),
    "scripts/smg_wheelhouse.py": (
        "2451ffbd103cc94b0b89592f5d40c02450e63a2f3460f3e859e2c813e8092962"
    ),
    "scripts/verify-smg-wheel-metadata.py": (
        "f79ff57ef7d6fb366948d4a6aeecd3244ee6937c1113bdd4f63e1eb091a68774"
    ),
    "scripts/build-sglang-faststart-image.sh": (
        "5efeb3410905da46ab76e8e8c14f54e526e7c8d059effac4ca62ddd41aee95d6"
    ),
    "scripts/prepare-sglang-faststart.sh": (
        "aa8413c0ec5438c97c4889e268af14488ca8f5e1b7fbc51c3ac71fea3716d0b4"
    ),
    "scripts/build-sglang-0515-queue-image.sh": (
        "e308dd1cd5007124395b197eaca1c30779c8e1d945d224ee2826926ab0a32782"
    ),
    "vendor/sglang-0515/apply_queue_patch.py": (
        "9c41fe9ae530f49a5eede4f88cbac5ffa81db7cfca6296ebdc737240aef8549c"
    ),
    "scripts/sglang-faststart-canary.sbatch": (
        "30623d14d5260e83bfb29d037de9a22fd17cd9123fe54df879099c50975857ef"
    ),
}
REVIEWED_DEPLOYED_PROGRAM_MODES = {
    "scripts/build-smg-image.sh": 0o755,
    "scripts/smg_wheelhouse.py": 0o644,
    "scripts/verify-smg-wheel-metadata.py": 0o644,
    "scripts/build-sglang-faststart-image.sh": 0o755,
    "scripts/prepare-sglang-faststart.sh": 0o755,
    "scripts/build-sglang-0515-queue-image.sh": 0o755,
    "vendor/sglang-0515/apply_queue_patch.py": 0o644,
    "scripts/sglang-faststart-canary.sbatch": 0o644,
}
MAX_DOWNLOAD_BYTES = 16 * 1024 * 1024
MAX_KIMI_SOURCE_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_KIMI_EXTRACTED_BYTES = 64 * 1024 * 1024
MAX_ERROR_DIAGNOSTIC_CHARS = 8 * 1024
IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

VALIDATOR_KINDS = {
    "control-image": "oci-image",
    "smg-docker-image": "oci-image",
    "smg-enroot-image": "artifact-fixture",
    "sglang-faststart-enroot-image": "artifact-fixture",
    "sglang-faststart-k2p-publisher": "artifact-fixture",
    "sglang-faststart-m1-publisher": "artifact-fixture",
    "sglang-faststart-canary-publisher": "artifact-fixture",
    "jit-seed-publisher": "artifact-fixture",
    "sglang-0515-enroot-image": "artifact-fixture",
    "sglang-glm53-image-contracts": "repository-contract",
    "schema-contracts": "repository-contract",
    "control-deployment-contracts": "repository-contract",
    "viewer-deployment-contracts": "repository-contract",
    "workflow-contracts": "repository-contract",
    "deployed-input-contracts": "repository-contract",
    "sglang-k2-contracts": "repository-contract",
}
VALID_CONTAINER_ENGINES_BY_KIND = {
    "artifact-fixture": frozenset({"podman"}),
    "oci-image": frozenset({"docker", "podman"}),
    "repository-contract": frozenset({"none"}),
}

REPOSITORY_VALIDATOR_COMMANDS = {
    "sglang-glm53-image-contracts": (
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_glm53_image.py",
            "tests/test_runtime_artifact.py",
            "tests/test_sglang_redaction_images.py",
        ),
    ),
    "sglang-k2-contracts": (
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_k2_sglang_image.py",
        ),
    ),
    "schema-contracts": (
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_schema_export.py",
            "tests/test_config.py",
            "tests/test_deployment_context.py",
            "tests/test_deployment_context_env.py",
            "tests/test_deployment_context_inventory.py",
            "tests/test_data_paths.py",
            "tests/test_docs_deployment_layout.py",
            "tests/test_image_probe_contract.py",
        ),
    ),
    "control-deployment-contracts": (
        ("bash", "-n", "scripts/install-control-venv.sh"),
        ("bash", "-n", "scripts/pull-to-cluster.sh"),
        ("bash", "-n", "scripts/setup-engine-venvs.sh"),
        ("bash", "-n", "scripts/hot-swap-gpu-monitors.sh"),
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_atomic_deploy.py",
            "tests/test_slurm_identity_probe.py",
        ),
    ),
    "viewer-deployment-contracts": (
        ("bash", "-n", "scripts/pull-to-viewer.sh"),
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_atomic_deploy.py",
            "tests/test_grafana_provisioning.py",
            "tests/test_telemetry_rollup_backfill.py",
            "tests/test_secondary_dashboard_rollups.py",
            "tests/test_ingest.py",
        ),
    ),
    "workflow-contracts": (
        ("actionlint", "-no-color", "-shellcheck=", "-pyflakes="),
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_ci_workflows.py",
            "tests/test_gitleaks_policy.py",
        ),
    ),
    "deployed-input-contracts": (
        (
            "uv",
            "run",
            "--locked",
            "--extra",
            "dev",
            "pytest",
            "-q",
            "tests/test_deployed_inputs.py",
            "tests/test_deployed_artifacts.py",
        ),
    ),
}

ACTIONLINT_VERSION = "1.7.12"
ACTIONLINT_ARCHIVES = {
    ("darwin", "amd64"): (
        "actionlint_1.7.12_darwin_amd64.tar.gz",
        "5b44c3bc2255115c9b69e30efc0fecdf498fdb63c5d58e17084fd5f16324c644",
    ),
    ("darwin", "arm64"): (
        "actionlint_1.7.12_darwin_arm64.tar.gz",
        "aba9ced2dee8d27fecca3dc7feb1a7f9a52caefa1eb46f3271ea66b6e0e6953f",
    ),
    ("linux", "amd64"): (
        "actionlint_1.7.12_linux_amd64.tar.gz",
        "8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8",
    ),
    ("linux", "arm64"): (
        "actionlint_1.7.12_linux_arm64.tar.gz",
        "325e971b6ba9bfa504672e29be93c24981eeb1c07576d730e9f7c8805afff0c6",
    ),
}


# Tool installation and container-engine preflight.
def _normalized_machine() -> str:
    machine = platform.machine().lower()
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    if machine in {"amd64", "x86_64"}:
        return "amd64"
    return machine


def _install_actionlint(destination: Path) -> Path:
    """Install the reviewed Actionlint binary from a bounded verified archive."""
    key = (platform.system().lower(), _normalized_machine())
    try:
        archive_name, expected_digest = ACTIONLINT_ARCHIVES[key]
    except KeyError as error:
        raise RuntimeError(f"Actionlint does not support this validator host: {key}") from error
    url = (
        "https://github.com/rhysd/actionlint/releases/download/"
        f"v{ACTIONLINT_VERSION}/{archive_name}"
    )
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("the Actionlint archive is too large")
    if hashlib.sha256(payload).hexdigest() != expected_digest:
        raise RuntimeError("the Actionlint archive checksum is incorrect")
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        members = [member for member in archive.getmembers() if member.name == "actionlint"]
        if len(members) != 1 or not members[0].isfile():
            raise RuntimeError("the Actionlint archive does not contain one binary")
        source = archive.extractfile(members[0])
        if source is None:
            raise RuntimeError("the Actionlint binary cannot be read")
        binary = source.read(MAX_DOWNLOAD_BYTES + 1)
    if len(binary) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("the Actionlint binary is too large")
    output = destination / "actionlint"
    output.write_bytes(binary)
    output.chmod(0o700)
    return output


def require_rootless_podman(
    *,
    runner: Any = subprocess.run,
    effective_uid: int | None = None,
) -> None:
    """Reject a root user or a rootful Podman service."""
    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid == 0:
        raise RuntimeError("artifact validation requires rootless Podman")
    result = runner(
        ["podman", "info", "--format", "{{.Host.Security.Rootless}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError("artifact validation requires rootless Podman")


def require_container_engine(
    container_engine: str,
    *,
    runner: Any = subprocess.run,
    effective_uid: int | None = None,
) -> None:
    """Require the selected container engine and a non-root client."""
    if container_engine == "podman":
        require_rootless_podman(runner=runner, effective_uid=effective_uid)
        return
    if container_engine != "docker":
        raise ValueError(f"unsupported container engine: {container_engine!r}")
    uid = os.geteuid() if effective_uid is None else effective_uid
    if uid == 0:
        raise RuntimeError("artifact validation requires a non-root Docker client")
    result = runner(
        ["docker", "info"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("artifact validation requires an available Docker engine")


def read_image_id(path: Path) -> str:
    """Read one bounded container image ID."""
    encoded = path.read_bytes()
    if len(encoded) > 256:
        raise ValueError("the container image ID is too large")
    image_id = encoded.decode("ascii").strip()
    if IMAGE_ID_PATTERN.fullmatch(image_id) is None:
        raise ValueError("the container engine returned an invalid image ID")
    return image_id


def _remove_image(image_id: str | None, runner: Any, container_engine: str) -> None:
    if image_id is not None:
        runner([container_engine, "image", "rm", image_id], check=False)


def _image_build_prefix(container_engine: str, *, pull: bool) -> list[str]:
    command = [container_engine, "build"]
    if container_engine == "podman":
        command.extend([f"--pull={'always' if pull else 'never'}", "--format=docker"])
    elif pull:
        command.append("--pull")
    return command


def _temporary_directory(root: Path | None, prefix: str) -> Any:
    if root is None:
        return tempfile.TemporaryDirectory(prefix=prefix)
    root.mkdir(parents=True, exist_ok=True)
    return nullcontext(str(root))


# OCI image validators.
def validate_control_image(
    repository_root: Path,
    *,
    container_engine: str,
    runner: Any = subprocess.run,
    temporary_root: Path | None = None,
) -> dict[str, str]:
    """Build and smoke-test the control image with the selected engine."""
    require_container_engine(container_engine, runner=runner)
    validate_control_recipe_contract(repository_root)
    image_id: str | None = None
    with _temporary_directory(temporary_root, "comet-control-image-") as temporary:
        iid_file = Path(temporary) / "control.iid"
        try:
            runner(
                [
                    *_image_build_prefix(container_engine, pull=True),
                    f"--platform={LINUX_PLATFORM}",
                    "--iidfile",
                    str(iid_file),
                    "--file",
                    str(repository_root / "docker" / "Dockerfile"),
                    str(repository_root),
                ],
                cwd=repository_root,
                check=True,
            )
            image_id = read_image_id(iid_file)
            isolation = [
                container_engine,
                "run",
                "--rm",
                f"--platform={LINUX_PLATFORM}",
                "--network=none",
                "--read-only",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                image_id,
            ]
            runner(
                [
                    *isolation,
                    "python",
                    "-c",
                    (
                        "from comet.cli.admin import MIGRATIONS_DIR; "
                        "from comet.k2_horizon import ("
                        "K2_ARTIFACT_COOKBOOKS_PATH, K2_HORIZON_DATA_DIR, "
                        "K2_SOURCE_MANIFESTS_DIR, "
                        "K2_SOURCE_PROFILES_PATH, list_k2_artifact_cookbooks, "
                        "list_k2_source_license_evidence, list_k2_source_profiles, "
                        "validate_k2_collection_comparison); "
                        "from comet.k2_source_admission import ("
                        "perform_after_active_publisher_license_acceptance, "
                        "require_active_publisher_license_acceptance); "
                        "from comet.paths import CLUSTERS_DIR; "
                        "from comet.pool import COOKBOOKS_DIR; "
                        "assert all(path.is_dir() for path in ("
                        "CLUSTERS_DIR, COOKBOOKS_DIR, K2_HORIZON_DATA_DIR, "
                        "K2_SOURCE_MANIFESTS_DIR, MIGRATIONS_DIR)); "
                        "assert all(path.is_file() for path in ("
                        "K2_ARTIFACT_COOKBOOKS_PATH, K2_SOURCE_PROFILES_PATH)); "
                        "validate_k2_collection_comparison(); "
                        "assert len(list_k2_source_profiles()) == "
                        "len(list_k2_artifact_cookbooks()) == "
                        "len(list_k2_source_license_evidence()) == 12; "
                        "assert callable(require_active_publisher_license_acceptance); "
                        "assert callable(perform_after_active_publisher_license_acceptance)"
                    ),
                ],
                check=True,
            )
            runner([*isolation, "comet", "--help"], check=True)
        finally:
            _remove_image(image_id, runner, container_engine)
    return {"image": "built", "runtime_data": "verified"}


def validate_control_recipe_contract(repository_root: Path) -> None:
    """Verify immutable control-image inputs and locked uv sync steps."""
    dockerfile = (repository_root / "docker" / "Dockerfile").read_text()
    required = (
        f"FROM --platform={LINUX_PLATFORM} {CONTROL_PYTHON_IMAGE_REF}",
        f"COPY --from={CONTROL_UV_IMAGE_REF} /uv /uvx /bin/",
        "COPY pyproject.toml README.md uv.lock ./",
        "COPY deployment/k2-horizon ./deployment/k2-horizon",
        "list_k2_source_profiles()",
        "list_k2_artifact_cookbooks()",
        "validate_k2_collection_comparison()",
        "RUN uv lock --check",
        "UV_PROJECT_ENVIRONMENT=/usr/local",
        "UV_PYTHON_DOWNLOADS=never",
        "RUN uv sync --locked --no-dev --no-editable --link-mode copy",
    )
    if any(token not in dockerfile for token in required):
        raise ValueError("the control image recipe pins are incorrect")


def build_smg_fixture_wheel(output: Path) -> str:
    """Build a deterministic SMG wheel with the production filename and tag."""
    flags = repr(SMG_ROUTER_FLAGS)
    router = f'''"""SMG router fixture for deployed-artifact validation."""
import argparse

FLAGS = {flags}


def parse_router_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", default="random")
    parser.add_argument("--max-cached-owners-per-prefix", type=int, default=1)
    parser.add_argument("--cache-owner-spill-cooldown-secs", type=int, default=0)
    parser.add_argument("--engine-metrics", action="store_true")
    parser.add_argument("--adaptive-admission-mode", default="off")
    parser.add_argument("--adaptive-admission-strategy", default="static")
    parser.add_argument("--prefer-trusted-tenant-header", action="store_true")
    parser.add_argument("--capacity-credit-generation", action="store_true")
    parser.add_argument("--priority-scheduler-adaptive-capacity", action="store_true")
    parser.add_argument("--adaptive-admission-distribution-headroom-partitions", action="append")
    parser.add_argument("--adaptive-admission-distribution-headroom-partition-seed-cap", type=int)
    parser.add_argument("--least-load-cache-mode", default="off")
    parser.add_argument("--least-load-default-throughput", type=float, default=0)
    parser.add_argument("--least-load-cache-prefill-throughput", type=float, default=0)
    parser.add_argument("--least-load-mean-remaining-decode-tokens", type=int, default=0)
    return parser.parse_args(argv)


def main():
    parse_router_args()


if __name__ == "__main__":
    main()
'''
    metadata = "Metadata-Version: 2.1\nName: smg\nVersion: 1.9.0\n" + "".join(
        f"Requires-Dist: {requirement}\n" for requirement in SMG_REQUIRES_DIST
    )
    files = {
        "smg/__init__.py": "",
        "smg/launch_router.py": router,
        "smg-1.9.0.dist-info/METADATA": metadata,
        "smg-1.9.0.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: comet-issue-523\n"
            "Root-Is-Purelib: false\nTag: cp38-abi3-manylinux_2_39_x86_64\n"
        ),
    }
    record_name = "smg-1.9.0.dist-info/RECORD"
    files[record_name] = "".join(f"{name},,\n" for name in (*files, record_name))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as wheel:
        for name, content in files.items():
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            wheel.writestr(info, content)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def _build_dependency_fixture_wheel(output: Path, name: str, version: str) -> str:
    """Build one deterministic pure-Python dependency wheel fixture."""
    distribution = name.replace("-", "_")
    metadata_directory = f"{distribution}-{version}.dist-info"
    files = {
        f"{distribution}/__init__.py": "",
        f"{metadata_directory}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
        ),
        f"{metadata_directory}/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: comet-fixture\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n"
        ),
    }
    record_name = f"{metadata_directory}/RECORD"
    files[record_name] = "".join(f"{path},,\n" for path in (*files, record_name))
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as wheel:
        for path, content in files.items():
            info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            wheel.writestr(info, content)
    return hashlib.sha256(output.read_bytes()).hexdigest()


def prepare_smg_fixture_context(
    repository_root: Path,
    context: Path,
    *,
    python_minor: str = "3.14",
) -> str:
    """Write a deterministic production-shaped SMG build context."""
    del repository_root
    wheel_digest = build_smg_fixture_wheel(context / SMG_WHEEL_NAME)
    wheelhouse = context / "docker" / "smg-wheelhouse"
    wheelhouse.mkdir(parents=True)
    requirements = (
        ("grpcio", "1.83.0"),
        ("grpcio-health-checking", "1.83.0"),
        ("protobuf", "7.35.1"),
        ("pyyaml", "6.0.3"),
        ("setproctitle", "1.3.7"),
        ("typing-extensions", "4.16.0"),
    )
    records: list[dict[str, Any]] = []
    for name, version in requirements:
        filename = f"{name.replace('-', '_')}-{version}-py3-none-any.whl"
        output = wheelhouse / filename
        digest = _build_dependency_fixture_wheel(output, name, version)
        records.append(
            {
                "filename": filename,
                "name": name,
                "python_minors": [python_minor],
                "sha256": digest,
                "size": output.stat().st_size,
                "url": f"https://files.pythonhosted.org/packages/fixture/{filename}",
                "version": version,
            }
        )
    lock = {
        "format": 1,
        "python_minors": [python_minor],
        "requirements": [{"name": name, "version": version} for name, version in requirements],
        "smg_requires_dist": list(SMG_REQUIRES_DIST),
        "wheels": records,
    }
    (context / "docker" / "smg-python-wheels.lock.json").write_text(
        json.dumps(lock, indent=2, sort_keys=True) + "\n"
    )
    return wheel_digest


def _download_smg_production_wheel(output: Path) -> None:
    """Download the bounded, byte-pinned production SMG wheel."""
    maximum = 64 * 1024 * 1024
    with urllib.request.urlopen(SMG_WHEEL_URL, timeout=60) as response:
        payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("the production SMG wheel is too large")
    if hashlib.sha256(payload).hexdigest() != SMG_WHEEL_SHA256:
        raise ValueError("the production SMG wheel SHA-256 is incorrect")
    output.write_bytes(payload)


def prepare_smg_production_context(
    repository_root: Path,
    context: Path,
    *,
    python_minor: str = "3.14",
) -> str:
    """Fetch the reviewed production SMG wheel and Python wheelhouse."""
    lock_source = repository_root / "docker" / "smg-python-wheels.lock.json"
    lock_target = context / "docker" / "smg-python-wheels.lock.json"
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(lock_source, lock_target)
    lock: DependencyLock = load_dependency_lock(lock_source)
    prepare_wheelhouse(
        lock,
        context / "docker" / "smg-wheelhouse",
        python_minor,
    )
    _download_smg_production_wheel(context / SMG_WHEEL_NAME)
    return SMG_WHEEL_SHA256


def validate_ubuntu_snapshot_packages(
    package_file: Path,
    *,
    snapshot: str,
    suites: tuple[str, ...],
    workspace: Path,
    runner: Any = subprocess.run,
) -> None:
    """Resolve each exact package pin from signed Ubuntu snapshot metadata."""
    workspace.mkdir(parents=True)
    lists = workspace / "lists"
    archives = workspace / "archives"
    (lists / "partial").mkdir(parents=True)
    (archives / "partial").mkdir(parents=True)
    sources = workspace / "ubuntu.sources"
    sources.write_text(
        "Types: deb\n"
        f"URIs: https://snapshot.ubuntu.com/ubuntu/{snapshot}/\n"
        f"Suites: {' '.join(suites)}\n"
        "Components: main universe\n"
        "Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n"
        "Check-Valid-Until: no\n"
    )
    options = [
        "-o",
        f"Dir::Etc::sourcelist={sources}",
        "-o",
        "Dir::Etc::sourceparts=-",
        "-o",
        f"Dir::State::lists={lists}",
        "-o",
        f"Dir::Cache::archives={archives}",
        "-o",
        "Dir::State::status=/var/lib/dpkg/status",
        "-o",
        "APT::Get::List-Cleanup=0",
        "-o",
        "Debug::NoLocking=1",
    ]
    runner(
        ["apt-get", *options, "update"],
        check=True,
        capture_output=True,
        text=True,
    )
    for pin in package_file.read_text().splitlines():
        expected_name, expected_version = pin.split("=", 1)
        result = runner(
            ["apt-cache", *options, "show", pin],
            check=True,
            capture_output=True,
            text=True,
        )
        paragraphs = result.stdout.split("\n\n")
        if not any(
            f"Package: {expected_name}" in paragraph.splitlines()
            and f"Version: {expected_version}" in paragraph.splitlines()
            for paragraph in paragraphs
        ):
            raise ValueError(f"the Ubuntu snapshot does not contain {pin}")


def _shell_assignments(
    path: Path,
    *,
    names: frozenset[str] | None = None,
) -> dict[str, str]:
    assignments: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=(.*)", line)
        if match is None:
            if names is None:
                raise ValueError(f"invalid assignment at {path}:{line_number}")
            continue
        name, value = match.groups()
        if names is not None and name not in names:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name in assignments or not value or "\0" in value:
            raise ValueError(f"invalid assignment at {path}:{line_number}")
        assignments[name] = value
    return assignments


def _smg_package_file_names(pattern: re.Pattern[str], text: str) -> tuple[str, ...]:
    """Return the SMG package-version file names referenced by one recipe."""
    package_file_names = tuple(sorted(set(pattern.findall(text))))
    if not package_file_names:
        raise ValueError("the SMG package-version files are missing")
    return package_file_names


def validate_smg_recipe_contract(repository_root: Path) -> None:
    """Require equal production pins in the SMG Docker and Enroot recipes."""
    _require_reviewed_programs(
        repository_root,
        "scripts/build-smg-image.sh",
        "scripts/smg_wheelhouse.py",
        "scripts/verify-smg-wheel-metadata.py",
    )
    dockerfile = (repository_root / "docker" / "smg.Dockerfile").read_text()
    recipe = repository_root / "scripts" / "build-smg-image.sh"
    docker_package_files = tuple(
        sorted((repository_root / "docker").glob("smg-apt-packages-*.txt"))
    )
    dependency_lock = repository_root / "docker" / "smg-python-wheels.lock.json"
    if not docker_package_files:
        raise ValueError("the SMG package-version files are missing")
    for package_file in docker_package_files:
        lines = package_file.read_text().splitlines()
        packages = [line.partition("=")[0] for line in lines]
        if (
            not lines
            or any(re.fullmatch(r"[a-z0-9][a-z0-9+.-]*=[^\s=]+", line) is None for line in lines)
            or len(packages) != len(set(packages))
            or packages.count("ca-certificates") != 1
        ):
            raise ValueError(f"the package-version file is invalid: {package_file.name}")
    if load_dependency_lock(dependency_lock).smg_requires_dist != SMG_REQUIRES_DIST:
        raise ValueError("the SMG Python dependency lock is incorrect")
    expected = {
        "SMG_IMAGE_VERSION": "1.9.0-prod-d636bcd1",
        "SMG_WHEEL": SMG_WHEEL_NAME,
        "SMG_RELEASE_TAG": "wheel/prod-d636bcd1",
        "SMG_SOURCE_SHA": "d636bcd17bec06372852e0b259b37c99248daf59",
        "SMG_WHEEL_SHA256": SMG_WHEEL_SHA256,
        "UBUNTU_IMAGE_REF": SMG_UBUNTU_ENROOT_REF,
        "UBUNTU_SNAPSHOT": SMG_ENROOT_UBUNTU_SNAPSHOT,
    }
    assignments = _shell_assignments(recipe, names=frozenset(expected))
    if {name: assignments.get(name) for name in expected} != expected:
        raise ValueError("the SMG Enroot recipe pins are incorrect")
    recipe_source = recipe.read_text()
    package_file_names = {
        *_smg_package_file_names(SMG_DOCKER_PACKAGE_FILE_PATTERN, dockerfile),
        *_smg_package_file_names(SMG_ENROOT_PACKAGE_FILE_PATTERN, recipe_source),
    }
    actual_package_file_names = {package_file.name for package_file in docker_package_files}
    if package_file_names != actual_package_file_names:
        raise ValueError("the SMG package-version files are not aligned with the recipes")
    required = (
        f"FROM --platform=linux/amd64 {SMG_DOCKER_UBUNTU_IMAGE_REF}",
        f"ARG SMG_WHEEL={SMG_WHEEL_NAME}",
        f"ARG SMG_WHEEL_SHA256={SMG_WHEEL_SHA256}",
        "COPY ${SMG_WHEEL} /tmp/",
        "COPY docker/smg-apt-packages-26.04.txt /tmp/smg-apt-packages-26.04.txt",
        "COPY docker/smg-python-wheels.lock.json /tmp/smg-python-wheels.lock.json",
        "COPY docker/smg-wheelhouse /tmp/smg-wheelhouse",
        "COPY scripts/smg_wheelhouse.py /tmp/smg_wheelhouse.py",
        "COPY scripts/verify-smg-wheel-metadata.py /tmp/verify-smg-wheel-metadata.py",
        f"https://snapshot.ubuntu.com/ubuntu/{SMG_DOCKER_UBUNTU_SNAPSHOT}/",
        "Suites: resolute resolute-updates resolute-security",
        'Acquire::https::Verify-Peer "false";',
        "rm -f /etc/apt/apt.conf.d/99snapshot-ca-bootstrap",
        "python3 /tmp/smg_wheelhouse.py",
        "python3 /tmp/verify-smg-wheel-metadata.py",
        "--dependency-lock /tmp/smg-python-wheels.lock.json",
        "--no-index --no-deps",
        "python3 -m pip check",
    )
    if any(token not in dockerfile for token in required):
        raise ValueError("the SMG Docker and Enroot recipe pins differ")
    required_recipe = (
        'base_dir="$(mktemp -d ',
        'enroot import -o "$base_image" "$UBUNTU_IMAGE_REF"',
        'enroot create -n "$BUILD" "$base_image"',
        '-m "$here:/comet:ro"',
        '-m "$wheelhouse:/wheelhouse:ro"',
        "python3 /comet/scripts/smg_wheelhouse.py",
        "python3 /comet/scripts/verify-smg-wheel-metadata.py",
        "--dependency-lock /comet/docker/smg-python-wheels.lock.json",
        "--no-index --no-deps",
        "https://snapshot.ubuntu.com/ubuntu/$UBUNTU_SNAPSHOT/",
        "Suites: noble noble-updates noble-security",
        "rm -f /etc/apt/apt.conf.d/99snapshot-ca-bootstrap",
        "python3 -m pip check",
    )
    forbidden_recipe = ("[ -f ubuntu2404.sqsh ] ||", 'enroot create -n "$BUILD" ubuntu2404.sqsh')
    if any(token not in recipe_source for token in required_recipe) or any(
        token in recipe_source for token in forbidden_recipe
    ):
        raise ValueError("the SMG Enroot recipe inputs are not immutable")
    subprocess.run(["bash", "-n", str(recipe)], check=True)


def validate_smg_docker_image(
    repository_root: Path,
    *,
    container_engine: str,
    runner: Any = subprocess.run,
    temporary_root: Path | None = None,
    artifact_preparer: Any = prepare_smg_production_context,
) -> dict[str, str]:
    """Build and execute the pinned production SMG image."""
    require_container_engine(container_engine, runner=runner)
    validate_smg_recipe_contract(repository_root)
    image_id: str | None = None
    with _temporary_directory(temporary_root, "comet-smg-image-") as temporary:
        context = Path(temporary)
        (context / "docker").mkdir()
        dockerfile = (repository_root / "docker" / "smg.Dockerfile").read_text()
        wheel_digest = artifact_preparer(repository_root, context)
        for package_file_name in _smg_package_file_names(
            SMG_DOCKER_PACKAGE_FILE_PATTERN, dockerfile
        ):
            shutil.copy2(
                repository_root / "docker" / package_file_name,
                context / "docker" / package_file_name,
            )
        (context / "scripts").mkdir()
        shutil.copy2(
            repository_root / "scripts" / "smg_wheelhouse.py",
            context / "scripts" / "smg_wheelhouse.py",
        )
        shutil.copy2(
            repository_root / "scripts" / "verify-smg-wheel-metadata.py",
            context / "scripts" / "verify-smg-wheel-metadata.py",
        )
        iid_file = context / "smg.iid"
        bad_iid_file = context / "smg-bad.iid"
        try:
            runner(
                [
                    *_image_build_prefix(container_engine, pull=True),
                    f"--platform={LINUX_PLATFORM}",
                    "--iidfile",
                    str(iid_file),
                    "--build-arg",
                    f"SMG_WHEEL_SHA256={wheel_digest}",
                    "--file",
                    str(repository_root / "docker" / "smg.Dockerfile"),
                    str(context),
                ],
                check=True,
            )
            image_id = read_image_id(iid_file)
            result = runner(
                [
                    container_engine,
                    "run",
                    "--rm",
                    f"--platform={LINUX_PLATFORM}",
                    "--network=none",
                    "--read-only",
                    "--cap-drop=all",
                    "--security-opt=no-new-privileges",
                    image_id,
                    "--help",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            missing = [flag for flag in SMG_ROUTER_FLAGS if flag not in result.stdout]
            if missing:
                raise ValueError(f"the SMG router help is missing options: {missing}")

            bad_result = runner(
                [
                    *_image_build_prefix(container_engine, pull=False),
                    f"--platform={LINUX_PLATFORM}",
                    "--iidfile",
                    str(bad_iid_file),
                    "--build-arg",
                    f"SMG_WHEEL_SHA256={'0' * 64}",
                    "--file",
                    str(repository_root / "docker" / "smg.Dockerfile"),
                    str(context),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            bad_image_id = bad_iid_file.read_text().strip() if bad_iid_file.exists() else ""
            if bad_image_id:
                if IMAGE_ID_PATTERN.fullmatch(bad_image_id):
                    _remove_image(bad_image_id, runner, container_engine)
                raise ValueError("a broken SMG wheel checksum produced an image ID")
            if bad_result.returncode == 0:
                raise ValueError("the SMG image accepted a broken wheel checksum")
            checksum_failure = "the wheel SHA-256 is incorrect"
            diagnostics = f"{bad_result.stdout or ''}\n{bad_result.stderr or ''}"
            if checksum_failure not in diagnostics:
                raise ValueError("the SMG checksum rejection was not proven")
        finally:
            _remove_image(image_id, runner, container_engine)
    return {
        "broken_checksum": "rejected",
        "image": "built",
        "production_wheel": "verified",
        "router_help": "verified",
    }


KIMI_UPSTREAM_SHA = "4051b19cc2cd7fd8903ceec1d084a69b56c6df4d"
KIMI_BASE_IMAGE_ALLOWLIST = (
    "6f02e62c94484277cdd676bb9b5b1266e9bf058c01e5f482beb9bb41955a4fc5",
    "bb1e504cffb8cb9e0e4c4201cdcf7f7a2f15d589823cfdf52b95b70e0afeb960",
)
KIMI_SOURCE_ARCHIVE_NAME = f"sglang-{KIMI_UPSTREAM_SHA}.tar.gz"
KIMI_SOURCE_ARCHIVE_URL = (
    "https://codeload.github.com/sgl-project/sglang/tar.gz/" + KIMI_UPSTREAM_SHA
)
KIMI_SOURCE_ARCHIVE_SHA256 = "2787e82eba4392a60a28e0b9057a977ab907b468341fa90ef73c1c7e587b7ea7"
KIMI_QUEUE_PATCH_DIGEST = "089a53fcb6a7b7bc285289db808c69c66fb4ddbf000efd05309f29acd860d45b"
KIMI_REDACTION_PATCH_DIGEST = "2d78ab4bcc377f00a8e70a5d7ec3540c59e58881c730b3ecb5d034b617fb856a"
KIMI_BOOTSTRAP_AUTH_PATCH_DIGEST = (
    "f29483ccc8e574d20c0ec648f7006383e7dc9671ac0574fb736d8bf7061d525c"
)
KIMI_PATCH_DIGESTS = {
    "0001-remote-instance-fast-start.patch": (
        "c66afc3e4ec1f03b14e2d1f0f703aec0d4c0215c6ddb84819dc54a164a8eebdd"
    ),
    "0002-decode-batch-telemetry.patch": (
        "6d4f97fb89038c9f9cfe5a3afd55e587eecce1332343780e394daf5a46a58805"
    ),
    "0003-bootstrap-timing.patch": (
        "71f159f902a09a98c25a1be85fbee315c63c8faeca5ded7ad74f9c9c288cfc50"
    ),
    "0004-kimi-time-weight-device.patch": (
        "02e5574418ba41f63c806fc84451b8f5205dae18b1fe9b332887f730f2fa2216"
    ),
    "0005-lustre-checkpoint-pipeline.patch": (
        "5ee8557db489cc044b56dc1437d67284c2f29715086de31ef2486b0d8a772b8b"
    ),
    "0006-lustre-local-staging.patch": (
        "2ff4394ec0efc00f68b82f87a479b379498f28d119bd7bab99d35064ed943993"
    ),
    "0007-pipeline-parallel-fast-start.patch": (
        "3831303234a0d8d40a1498902bc7358e52bf670cefd3107f5510fb0570c97878"
    ),
    "0008-pipeline-local-credit-layers.patch": (
        "252777374272445f674ea8eb1b4277df60728261a315fb92249b8ca57fbd106f"
    ),
    "0009-included-router-request-path.patch": (
        "440695ad8b31287482866e9d8ea36b324f4ee665b74379c2afd28e15c58ef9f1"
    ),
    "0009-kimi-k3-cross-node-fast-start.patch": (
        "d82299f86c14773dc4c4ae9d5190d4468cf09d2ec0aa01855ee1d861c38d8019"
    ),
    "0010-Fix-fast-start-tensor-hash-import-for-Kimi-K3-base.patch": (
        "ee10436fa9acca2dddc8e119fabc0b90cdf1b13b78618ec997d48e62aadec620"
    ),
    "0011-Adapt-fast-start-tests-to-Kimi-K3-worker-guards.patch": (
        "864558b7690fbd7c964aa3758f888116856f280538686cc2b5ac76b813b1167d"
    ),
    "0012-Skip-legacy-NCCL-group-pretrigger-for-fast-start.patch": (
        "2c32ef4d5945da0e0135a896e443e75309f97c9f8efb3f2198fbc42c286690ed"
    ),
    "0013-Share-fast-start-NCCL-client-ID-across-nodes.patch": (
        "f035b8ba623a4ef01e9ffb69719e5fd020694ff7fa6d987ca0901c4be47f6b2a"
    ),
    "0014-Transport-fast-start-tensors-as-raw-NCCL-bytes.patch": (
        "4f1952cb44b9a07da290e40d0c9314663aabffebd323d675bada2cf096a6afe9"
    ),
    "0015-Preserve-symmetric-memory-across-fast-start-teardown.patch": (
        "2d8fde6aca36ae39fc9ed366e4c3b33a54a3a99a0140176dde8f7e5c5c0c2e10"
    ),
    "0016-Retain-fast-start-side-group-through-graph-capture.patch": (
        "d5aba0777a14dc088472d72805315273c52c86a727ff8e9e1fa42087410bee67"
    ),
    "0017-Preallocate-fast-start-symmetric-memory-before-capture.patch": (
        "50b6116840aa5ddb83cae915aca47071d9761d4cace315ddcf47f9d41c40907d"
    ),
    "0018-Preallocate-symmetric-memory-on-final-capture-stream.patch": (
        "5f9cc04298df413fe971917ab2703f724557d33e99812e0f2a3cd040089fc1d1"
    ),
    "0019-Refresh-Kimi-K3-derived-state-after-transfer.patch": (
        "9b4e0e2cf8426a39c4477c5e32abdcafecc0fa1a5be52002cb76dd2a77d03582"
    ),
    "0020-authenticate-donor-control-requests.patch": (
        "c4ce8b750d8e581d46c715b9ee85789702ab277ebfad88bf5ea397336510d525"
    ),
    "0021-queue-observability.patch": KIMI_QUEUE_PATCH_DIGEST,
    "0022-server-args-redaction.patch": KIMI_REDACTION_PATCH_DIGEST,
    "0023-authenticate-fast-start-bootstrap.patch": KIMI_BOOTSTRAP_AUTH_PATCH_DIGEST,
}
SGLANG_0515_UPSTREAM_SHA = "0b3bb0cbe31873994c9f989fddfe2f87ca839fdd"
SGLANG_0515_PATCH_DIGESTS = {
    "0001-queue-observability.patch": (
        "d8f7ed3888688a584c7dd35ecb2bf2caa7871c92f962ac432f00a77edebf91c9"
    ),
    "0002-server-args-redaction.patch": (
        "cbd99b83bb760a4988d09d4adeff5cfe694c9d376d47cdeffb3266922cfc3666"
    ),
    "0003-faststart-server-args-redaction.patch": (
        "11632a9366b3c086bd14cfa2db168e581cd94df35b66060b9adc341a724db06b"
    ),
}
SGLANG_0515_REDACTION_PATCH_DIGEST = SGLANG_0515_PATCH_DIGESTS["0002-server-args-redaction.patch"]
SGLANG_0515_MANIFEST_DIGEST = "91e3d8384ecf740fb573f48f65a02ad20ed1ca83ce49bde1c3b6f9d9094ddc03"
SGLANG_0515_SOURCE_FILES = (
    "python/sglang/srt/managers/scheduler.py",
    "python/sglang/srt/managers/scheduler_components/metrics_reporter.py",
    "python/sglang/srt/observability/metrics_collector.py",
    "python/sglang/srt/server_args.py",
    "python/sglang/srt/entrypoints/engine.py",
    "python/sglang/srt/entrypoints/http_server.py",
)
SGLANG_0515_BASE_IMAGE_ALLOWLIST = (
    "b10e37dc77697cfc0e3f8bc976bd4753b848772a669faa8c298f6fa1a0eaeed9",
    "9efbaf3a38ef1fcfca4c20165bc87393c7b3e8199434e109f83355177a283881",
    "0e341cdd30960ef55d8aebb376401406f890e6a3001d781b472353b0b88c5797",
    "0c30bb062c16dcbada002266453488dd18072e46e1211844e3e2d64638ba1d2d",
    "7075cecf11658cdee3e94b124528f5d48832e4d06509e9a75904353e13125826",
    "4b6a25962b3dd983455ae4a9c690c57e367b0923aa5cb6c7da6522a8d8fb859a",
    "0118659bd0ac96948c0e14a5e30302564fce65c309d9fadac61658b4ab4fd357",
)
SGLANG_0515_STOCK_REDACTION_BASE_IMAGE_ALLOWLIST = (
    "b10e37dc77697cfc0e3f8bc976bd4753b848772a669faa8c298f6fa1a0eaeed9",
    "9efbaf3a38ef1fcfca4c20165bc87393c7b3e8199434e109f83355177a283881",
    "0118659bd0ac96948c0e14a5e30302564fce65c309d9fadac61658b4ab4fd357",
)


# Reviewed source, patch, and SquashFS helpers.
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _download_kimi_source_archive(output: Path) -> None:
    with urllib.request.urlopen(KIMI_SOURCE_ARCHIVE_URL, timeout=60) as response:
        payload = response.read(MAX_KIMI_SOURCE_ARCHIVE_BYTES + 1)
    if len(payload) > MAX_KIMI_SOURCE_ARCHIVE_BYTES:
        raise RuntimeError("the Kimi-K3 source archive is too large")
    if hashlib.sha256(payload).hexdigest() != KIMI_SOURCE_ARCHIVE_SHA256:
        raise RuntimeError("the Kimi-K3 source archive checksum is incorrect")
    output.write_bytes(payload)


def _require_reviewed_programs(repository_root: Path, *relative_paths: str) -> None:
    """Require each deployed program to have its reviewed type, mode, and digest."""
    for relative_path in relative_paths:
        try:
            expected = REVIEWED_DEPLOYED_PROGRAM_DIGESTS[relative_path]
            expected_mode = REVIEWED_DEPLOYED_PROGRAM_MODES[relative_path]
        except KeyError as error:
            raise ValueError(
                f"the deployed program has no reviewed digest: {relative_path}"
            ) from error
        path = repository_root / relative_path
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError as error:
            raise ValueError(
                f"the deployed program is not a reviewed regular file: {relative_path}"
            ) from error
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError(
                    f"the deployed program is not a reviewed regular file: {relative_path}"
                )
            if stat.S_IMODE(metadata.st_mode) != expected_mode:
                raise ValueError(f"the deployed program mode is not reviewed: {relative_path}")
            digest = hashlib.sha256()
            while block := os.read(descriptor, 64 * 1024):
                digest.update(block)
        finally:
            os.close(descriptor)
        if digest.hexdigest() != expected:
            raise ValueError(f"the deployed program digest is not reviewed: {relative_path}")


def _materialize_patch_preimage(
    patch: Path,
    destination: Path,
    *,
    apply: bool = True,
) -> set[str]:
    """Generate exact hunk preimages and optionally apply the real patch."""
    source = patch.read_text()
    git_format = re.search(r"^diff --git ", source, flags=re.MULTILINE) is not None
    separator = r"(?=^diff --git )" if git_format else r"(?=^--- a/)"
    sections = re.split(separator, source, flags=re.MULTILINE)
    touched: set[str] = set()
    destination.mkdir(parents=True, exist_ok=True)
    for section in sections:
        header = re.match(r"diff --git a/(\S+) b/(\S+)\n", section)
        if header is None and not git_format:
            header = re.match(r"--- a/(\S+)\n\+\+\+ b/(\S+)\n", section)
        if header is None:
            continue
        old_path, new_path = header.groups()
        if old_path != new_path:
            raise ValueError(f"patch renames are not supported: {patch.name}")
        if old_path.startswith("/") or ".." in Path(old_path).parts:
            raise ValueError(f"unsafe patch path: {old_path}")
        touched.add(old_path)
        target = destination / old_path
        lines: list[str | None] = []
        hunks = list(
            re.finditer(
                r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*\n",
                section,
                flags=re.MULTILINE,
            )
        )
        for index, hunk in enumerate(hunks):
            old_start = int(hunk.group(1))
            old_count = int(hunk.group(2) or "1")
            new_count = int(hunk.group(4) or "1")
            end = hunks[index + 1].start() if index + 1 < len(hunks) else len(section)
            body = section[hunk.end() : end].splitlines(keepends=True)
            preimage: list[str] = []
            old_seen = 0
            new_seen = 0
            for line in body:
                if old_seen == old_count and new_seen == new_count:
                    break
                if not line.startswith((" ", "+", "-")):
                    continue
                if line.startswith((" ", "-")):
                    old_seen += 1
                    preimage.append(line[1:])
                if line.startswith((" ", "+")):
                    new_seen += 1
            if old_seen != old_count or new_seen != new_count:
                raise ValueError(f"cannot generate a patch hunk preimage: {patch.name}")
            if old_count == 0:
                continue
            offset = old_start - 1
            while len(lines) < offset + old_count:
                lines.append(None)
            for line_offset, content in enumerate(preimage):
                position = offset + line_offset
                if lines[position] not in (None, content):
                    raise ValueError(f"patch hunks have different preimages: {patch.name}")
                lines[position] = content
        if lines:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "".join(
                    content if content is not None else f"# COMET unknown preimage line {number}\n"
                    for number, content in enumerate(lines, start=1)
                )
            )
    if not touched:
        raise ValueError(f"patch contains no file payloads: {patch.name}")
    subprocess.run(
        ["git", "apply", "--check", str(patch.resolve())],
        cwd=destination,
        check=True,
        capture_output=True,
        text=True,
    )
    if apply:
        subprocess.run(
            ["git", "apply", str(patch.resolve())],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
    return touched


def _patch_payload_paths(patch: Path) -> set[str]:
    source = patch.read_text()
    matches = re.findall(
        r"^diff --git a/(\S+) b/(\S+)$",
        source,
        flags=re.MULTILINE,
    )
    if not matches:
        matches = re.findall(
            r"^--- a/(\S+)\n\+\+\+ b/(\S+)$",
            source,
            flags=re.MULTILINE,
        )
    if not matches:
        raise ValueError(f"patch contains no file payloads: {patch.name}")
    touched: set[str] = set()
    for old_path, new_path in matches:
        if old_path != new_path:
            raise ValueError(f"patch renames are not supported: {patch.name}")
        if old_path.startswith("/") or ".." in Path(old_path).parts:
            raise ValueError(f"unsafe patch path: {old_path}")
        touched.add(old_path)
    return touched


def _apply_ordered_patch_stack(patches: list[Path], destination: Path) -> set[str]:
    """Apply the complete patch stack to one reviewed source tree."""
    touched: set[str] = set()
    for patch in patches:
        touched.update(_patch_payload_paths(patch))
        result = subprocess.run(
            ["git", "apply", "--check", str(patch.resolve())],
            cwd=destination,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise ValueError(f"ordered patch preimage differs: {patch.name}")
        subprocess.run(
            ["git", "apply", str(patch.resolve())],
            cwd=destination,
            check=True,
            capture_output=True,
            text=True,
        )
    return touched


def _extract_kimi_source_files(
    archive_path: Path,
    destination: Path,
    relative_paths: set[str],
) -> None:
    """Extract only reviewed regular paths from the verified Kimi-K3 archive."""
    if archive_path.is_symlink() or not archive_path.is_file():
        raise ValueError("the Kimi-K3 source archive is not a regular file")
    if _sha256(archive_path) != KIMI_SOURCE_ARCHIVE_SHA256:
        raise ValueError("the Kimi-K3 source archive checksum is incorrect")
    archive_prefix = f"sglang-{KIMI_UPSTREAM_SHA}/"
    expected_names = {archive_prefix + path: path for path in relative_paths}
    extracted_bytes = 0
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:gz") as archive:
        selected = [member for member in archive.getmembers() if member.name in expected_names]
        if len({member.name for member in selected}) != len(selected):
            raise ValueError("the Kimi-K3 source archive has duplicate selected paths")
        for member in selected:
            if not member.isfile() or member.size > MAX_DOWNLOAD_BYTES:
                raise ValueError("the Kimi-K3 source archive has an unsafe selected path")
            extracted_bytes += member.size
            if extracted_bytes > MAX_KIMI_EXTRACTED_BYTES:
                raise ValueError("the selected Kimi-K3 source files are too large")
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("a selected Kimi-K3 source file cannot be read")
            payload = source.read(MAX_DOWNLOAD_BYTES + 1)
            if len(payload) != member.size:
                raise ValueError("a selected Kimi-K3 source file has an invalid size")
            target = destination / expected_names[member.name]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            target.chmod(stat.S_IMODE(member.mode))


def _make_squashfs(rootfs: Path, image: Path) -> None:
    if image.exists() or image.is_symlink():
        raise ValueError(f"SquashFS output already exists: {image}")
    subprocess.run(
        [
            "mksquashfs",
            str(rootfs),
            str(image),
            "-noappend",
            "-quiet",
            "-processors",
            "1",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _read_squashfs_file(image: Path, path: str) -> str:
    return subprocess.run(
        ["unsquashfs", "-cat", str(image), path],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_fixture_manifest(
    rootfs: Path,
    *,
    name: str,
    upstream_sha: str,
    patches: dict[str, str],
    extra: dict[str, Any],
) -> str:
    payload = {
        "name": name,
        "patches": patches,
        "upstream_sha": upstream_sha,
        **extra,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    manifest = rootfs / "etc" / "comet-deployed-artifact.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(encoded)
    return encoded


# Enroot artifact fixtures.
def validate_smg_enroot_fixture(repository_root: Path, workspace: Path) -> dict[str, str]:
    """Install the SMG payload and inspect a rootless SquashFS fixture."""
    validate_smg_recipe_contract(repository_root)
    recipe = (repository_root / "scripts" / "build-smg-image.sh").read_text()
    required = (
        'enroot export -o "$OUT_SQSH" "$BUILD"',
        "Output already exists",
        "parse_router_args",
    )
    if any(token not in recipe for token in required):
        raise ValueError("the SMG Enroot install or output contract is incomplete")
    production = workspace / "smg-production-inputs"
    wheel = production / SMG_WHEEL_NAME
    fixture_lock = production / "docker" / "smg-python-wheels.lock.json"
    dependency_wheelhouse = production / "docker" / "smg-wheelhouse"
    verify_wheelhouse(load_dependency_lock(fixture_lock), dependency_wheelhouse, "3.12")
    fixture = workspace / "smg-enroot"
    fixture.mkdir()
    subprocess.run(
        [
            sys.executable,
            str(repository_root / "scripts" / "verify-smg-wheel-metadata.py"),
            "--wheel",
            str(wheel),
            "--sha256",
            SMG_WHEEL_SHA256,
            "--name",
            "smg",
            "--version",
            "1.9.0",
            "--dependency-lock",
            str(fixture_lock),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    package_root = fixture / "rootfs" / "usr" / "local" / "lib" / "python3.12" / "site-packages"
    package_root.mkdir(parents=True)
    dependency_wheels = sorted(dependency_wheelhouse.glob("*.whl"))
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(package_root),
            *(str(path) for path in dependency_wheels),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--target",
            str(package_root),
            str(wheel),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    parser_probe = """from smg.launch_router import parse_router_args
args = parse_router_args([
    "--policy", "cache_aware",
    "--max-cached-owners-per-prefix", "8",
    "--cache-owner-spill-cooldown-secs", "5",
    "--adaptive-admission-mode", "enforce",
    "--adaptive-admission-strategy", "engine_feedback",
    "--adaptive-admission-distribution-headroom-partitions", "kimi-k3",
    "--adaptive-admission-distribution-headroom-partition-seed-cap", "1",
    "--least-load-cache-mode", "shadow",
    "--least-load-default-throughput", "200",
    "--least-load-cache-prefill-throughput", "8000",
    "--least-load-mean-remaining-decode-tokens", "2048",
])
assert args.policy == "cache_aware"
assert args.max_cached_owners_per_prefix == 8
assert args.cache_owner_spill_cooldown_secs == 5
assert args.adaptive_admission_mode == "enforce"
assert args.adaptive_admission_strategy == "engine_feedback"
assert args.adaptive_admission_distribution_headroom_partitions == ["kimi-k3"]
assert args.adaptive_admission_distribution_headroom_partition_seed_cap == 1
assert args.least_load_cache_mode == "shadow"
assert args.least_load_default_throughput == 200.0
assert args.least_load_cache_prefill_throughput == 8000.0
assert args.least_load_mean_remaining_decode_tokens == 2048
assert not args.priority_scheduler_adaptive_capacity
"""
    environment = {**os.environ, "PYTHONPATH": str(package_root)}
    subprocess.run(
        [sys.executable, "-c", parser_probe],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    help_output = subprocess.run(
        [sys.executable, "-m", "smg.launch_router", "--help"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    if any(flag not in help_output for flag in SMG_ROUTER_FLAGS):
        raise ValueError("the installed SMG router does not have all required options")
    rootfs = fixture / "rootfs"
    manifest = _write_fixture_manifest(
        rootfs,
        name="smg-enroot",
        upstream_sha="d636bcd17bec06372852e0b259b37c99248daf59",
        patches={},
        extra={"production_wheel_sha256": SMG_WHEEL_SHA256},
    )
    image = fixture / "smg-fixture.sqsh"
    _make_squashfs(rootfs, image)
    if _read_squashfs_file(image, "etc/comet-deployed-artifact.json") != manifest:
        raise ValueError("the SMG SquashFS manifest is incorrect")
    installed = _read_squashfs_file(
        image,
        "usr/local/lib/python3.12/site-packages/smg/launch_router.py",
    )
    if "parse_router_args" not in installed:
        raise ValueError("the SMG SquashFS does not contain the installed router")
    try:
        _make_squashfs(rootfs, image)
    except ValueError as error:
        if not image.is_file():
            raise ValueError("the SMG fixture did not keep its existing output") from error
    else:
        raise ValueError("the SMG fixture overwrote an existing output")
    return {"manifest": "verified", "router": "verified", "squashfs": _sha256(image)}


def _validate_kimi_recipe(repository_root: Path) -> list[Path]:
    """Verify the programs, pins, and ordered patch inventory for Kimi-K3."""
    _require_reviewed_programs(
        repository_root,
        "scripts/build-sglang-faststart-image.sh",
        "scripts/prepare-sglang-faststart.sh",
    )
    recipe = repository_root / "scripts" / "build-sglang-faststart-image.sh"
    preparation = repository_root / "scripts" / "prepare-sglang-faststart.sh"
    subprocess.run(["bash", "-n", str(recipe)], check=True)
    subprocess.run(["bash", "-n", str(preparation)], check=True)
    recipe_source = recipe.read_text()
    preparation_source = preparation.read_text()
    required_recipe = (
        'source "$repo_root/vendor/sglang/UPSTREAM.env"',
        "for patch in /comet-patches/*.patch",
        'enroot export -o "$output_image" "$build_name"',
        "/etc/comet-sglang-fast-start-revision",
        "/etc/comet-sglang-queue-revision",
        "/etc/comet-sglang-server-args-redaction-revision",
        "/etc/comet-sglang-bootstrap-auth-revision",
        "refusing to overwrite output image",
    )
    required_preparation = (
        "https://github.com/sgl-project/sglang.git",
        'checkout --detach "$SGLANG_UPSTREAM_SHA"',
        "vendor/sglang/patches/*.patch",
    )
    if any(token not in recipe_source for token in required_recipe) or any(
        token not in preparation_source for token in required_preparation
    ):
        raise ValueError("the Kimi-K3 build or preparation contract is incomplete")
    pins = _shell_assignments(repository_root / "vendor" / "sglang" / "UPSTREAM.env")
    expected = {
        "SGLANG_UPSTREAM_TAG": "kimi-k3",
        "SGLANG_UPSTREAM_SHA": KIMI_UPSTREAM_SHA,
        "SGLANG_BASE_IMAGE_SHA256_ALLOWLIST": " ".join(KIMI_BASE_IMAGE_ALLOWLIST),
        "SGLANG_PREPATCHED_BASE_IMAGE_SHA256": (
            "aefa29a6f3aed74e08848536b8e036a6f28cb63275d20df6a23877e87e8e5426"
        ),
        "SGLANG_KIMI_K3_BLOB": "b7f61cf2c56f98de85fd11243c15cf7cf6afd516",
        "SGLANG_MODEL_RUNNER_BLOB": "3d668b2f2e708ba10b0f50b648f1a0c11fd23399",
    }
    if pins != expected:
        raise ValueError("the Kimi-K3 upstream pins are not the reviewed values")
    patches = sorted((repository_root / "vendor" / "sglang" / "patches").glob("*.patch"))
    if any(patch.is_symlink() or not patch.is_file() for patch in patches):
        raise ValueError("the Kimi-K3 patch inventory contains a non-regular file")
    actual = {patch.name: _sha256(patch) for patch in patches}
    if actual != KIMI_PATCH_DIGESTS:
        raise ValueError("the Kimi-K3 patch inventory or digest is not reviewed")
    return patches


def validate_sglang_faststart_fixture(
    repository_root: Path,
    workspace: Path,
) -> dict[str, str]:
    """Apply all reviewed Kimi-K3 patches and inspect their SquashFS manifest."""
    patches = _validate_kimi_recipe(repository_root)
    fixture = workspace / "sglang-faststart"
    fixture.mkdir()
    rootfs = fixture / "rootfs"
    patch_root = fixture / "reviewed-source"
    patch_paths = set().union(*(_patch_payload_paths(patch) for patch in patches))
    _extract_kimi_source_files(
        workspace / KIMI_SOURCE_ARCHIVE_NAME,
        patch_root,
        patch_paths,
    )
    touched = _apply_ordered_patch_stack(patches, patch_root)
    installed_fixture = rootfs / "opt" / "comet" / "patch-fixture"
    for relative_path in sorted(touched):
        source = patch_root / relative_path
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"a patched Kimi-K3 source is not a regular file: {relative_path}")
        target = installed_fixture / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    if len(touched) != 42:
        raise ValueError(f"the Kimi-K3 patch stack touches {len(touched)} paths, not 42")
    revision = rootfs / "etc" / "comet-sglang-fast-start-revision"
    revision.parent.mkdir(parents=True, exist_ok=True)
    revision.write_text(KIMI_UPSTREAM_SHA + "\n")
    queue_revision = rootfs / "etc" / "comet-sglang-queue-revision"
    queue_revision.write_text(KIMI_QUEUE_PATCH_DIGEST + "\n")
    redaction_revision = rootfs / "etc" / "comet-sglang-server-args-redaction-revision"
    redaction_revision.write_text(KIMI_REDACTION_PATCH_DIGEST + "\n")
    auth_revision = rootfs / "etc" / "comet-sglang-bootstrap-auth-revision"
    auth_revision.write_text(KIMI_BOOTSTRAP_AUTH_PATCH_DIGEST + "\n")
    manifest = _write_fixture_manifest(
        rootfs,
        name="sglang-faststart",
        upstream_sha=KIMI_UPSTREAM_SHA,
        patches=KIMI_PATCH_DIGESTS,
        extra={
            "source_archive_sha256": KIMI_SOURCE_ARCHIVE_SHA256,
            "touched_paths": sorted(touched),
        },
    )
    image = fixture / "sglang-faststart-fixture.sqsh"
    _make_squashfs(rootfs, image)
    if (
        _read_squashfs_file(image, "etc/comet-sglang-fast-start-revision").strip()
        != KIMI_UPSTREAM_SHA
    ):
        raise ValueError("the Kimi-K3 SquashFS upstream marker is incorrect")
    if (
        _read_squashfs_file(image, "etc/comet-sglang-queue-revision").strip()
        != KIMI_QUEUE_PATCH_DIGEST
    ):
        raise ValueError("the Kimi-K3 SquashFS queue marker is incorrect")
    if (
        _read_squashfs_file(image, "etc/comet-sglang-server-args-redaction-revision").strip()
        != KIMI_REDACTION_PATCH_DIGEST
    ):
        raise ValueError("the Kimi-K3 SquashFS redaction marker is incorrect")
    if (
        _read_squashfs_file(image, "etc/comet-sglang-bootstrap-auth-revision").strip()
        != KIMI_BOOTSTRAP_AUTH_PATCH_DIGEST
    ):
        raise ValueError("the Kimi-K3 SquashFS bootstrap-authentication marker is incorrect")
    if _read_squashfs_file(image, "etc/comet-deployed-artifact.json") != manifest:
        raise ValueError("the Kimi-K3 SquashFS manifest is incorrect")
    return {
        "manifest": "verified",
        "patches_applied": str(len(patches)),
        "squashfs": _sha256(image),
        "touched_paths": str(len(touched)),
    }


def _production_0515_data(
    repository_root: Path,
) -> tuple[list[str], list[list[str]], list[Path]]:
    """Return the reviewed base images, source lineages, and patch stack."""
    vendor = repository_root / "vendor" / "sglang-0515"
    pins = _shell_assignments(vendor / "UPSTREAM.env")
    expected_pins = {
        "SGLANG_UPSTREAM_TAG": "v0.5.15.post1",
        "SGLANG_UPSTREAM_SHA": SGLANG_0515_UPSTREAM_SHA,
        "SGLANG_BASE_IMAGE_SHA256_ALLOWLIST": " ".join(SGLANG_0515_BASE_IMAGE_ALLOWLIST),
        "SGLANG_STOCK_REDACTION_BASE_IMAGE_SHA256_ALLOWLIST": " ".join(
            SGLANG_0515_STOCK_REDACTION_BASE_IMAGE_ALLOWLIST
        ),
    }
    if pins != expected_pins:
        raise ValueError("the SGLang 0.5.15 upstream pin is incorrect")
    allowlist = list(SGLANG_0515_BASE_IMAGE_ALLOWLIST)
    manifest_path = vendor / "SOURCE_SHA256"
    patches = sorted((vendor / "patches").glob("*.patch"))
    if any(patch.is_symlink() or not patch.is_file() for patch in patches):
        raise ValueError("the SGLang 0.5.15 patch inventory is not reviewed")
    actual_patch_digests = {patch.name: _sha256(patch) for patch in patches}
    if actual_patch_digests != SGLANG_0515_PATCH_DIGESTS:
        raise ValueError("the SGLang 0.5.15 patch inventory or digest is not reviewed")
    if _sha256(manifest_path) != SGLANG_0515_MANIFEST_DIGEST:
        raise ValueError("the SGLang 0.5.15 source manifest digest is not reviewed")
    rows = [
        line.split()
        for line in manifest_path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if len(rows) != 7 or any(
        len(row) != 13 or any(SHA256_PATTERN.fullmatch(value) is None for value in row)
        for row in rows
    ):
        raise ValueError("the SGLang 0.5.15 source manifest has invalid rows")
    if {row[0] for row in rows} != set(allowlist):
        raise ValueError("the SGLang 0.5.15 manifest does not cover the allowlist")
    if len({tuple(row[1:7]) for row in rows}) != 3 or len({tuple(row[7:]) for row in rows}) != 3:
        raise ValueError("the SGLang 0.5.15 manifest must contain three reviewed lineages")
    return allowlist, rows, patches


def validate_sglang_0515_fixture(repository_root: Path, workspace: Path) -> dict[str, str]:
    """Execute the verified 0.5.15 patch helper and inspect a SquashFS fixture."""
    _require_reviewed_programs(
        repository_root,
        "scripts/build-sglang-0515-queue-image.sh",
        "vendor/sglang-0515/apply_queue_patch.py",
    )
    recipe = repository_root / "scripts" / "build-sglang-0515-queue-image.sh"
    subprocess.run(["bash", "-n", str(recipe)], check=True)
    recipe_source = recipe.read_text()
    required = (
        "/comet-sglang-0515/apply_queue_patch.py",
        "/comet-sglang-0515/SOURCE_SHA256",
        'enroot export -o "$output_image" "$build_name"',
        "/etc/comet-sglang-queue-revision",
        "/etc/comet-sglang-server-args-redaction-revision",
        "refusing to overwrite output image",
        "0002-server-args-redaction.patch",
        "0003-faststart-server-args-redaction.patch",
    )
    if any(token not in recipe_source for token in required):
        raise ValueError("the SGLang 0.5.15 build contract is incomplete")
    allowlist, rows, patches = _production_0515_data(repository_root)
    queue_patch, stock_redaction_patch, faststart_redaction_patch = patches
    if _patch_payload_paths(stock_redaction_patch) != _patch_payload_paths(
        faststart_redaction_patch
    ):
        raise ValueError("the SGLang 0.5.15 redaction patch targets do not match")
    helper = repository_root / "vendor" / "sglang-0515" / "apply_queue_patch.py"
    variant_digests: dict[str, str] = {}
    redaction_variants = {
        "stock": stock_redaction_patch,
        "faststart": faststart_redaction_patch,
    }
    for variant, redaction_patch in redaction_variants.items():
        fixture_patches = [queue_patch, redaction_patch]
        fixture = workspace / f"sglang-0515-{variant}"
        fixture.mkdir()
        preimage = fixture / "preimage"
        touched: set[str] = set()
        for patch in fixture_patches:
            touched.update(_materialize_patch_preimage(patch, preimage, apply=False))
        if len(touched) != 7:
            raise ValueError("the SGLang 0.5.15 patch stack must touch seven files")
        before = [_sha256(preimage / path) for path in SGLANG_0515_SOURCE_FILES]
        postimage = fixture / "postimage"
        shutil.copytree(preimage, postimage)
        for patch in fixture_patches:
            subprocess.run(
                ["git", "apply", str(patch.resolve())],
                cwd=postimage,
                check=True,
                capture_output=True,
                text=True,
            )
        after = [_sha256(postimage / path) for path in SGLANG_0515_SOURCE_FILES]
        base_digest = "f" * 64
        synthetic_manifest = fixture / "SOURCE_SHA256"
        synthetic_manifest.write_text(" ".join([base_digest, *before, *after]) + "\n")
        verified = fixture / "verified"
        shutil.copytree(preimage, verified)
        subprocess.run(
            [
                sys.executable,
                str(helper),
                "--source-root",
                str(verified),
                "--base-image-sha256",
                base_digest,
                "--manifest",
                str(synthetic_manifest),
                *(str(patch) for patch in fixture_patches),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        if [_sha256(verified / path) for path in SGLANG_0515_SOURCE_FILES] != after:
            raise ValueError("the SGLang 0.5.15 verified patch output is incorrect")
        rootfs = fixture / "rootfs"
        shutil.copytree(verified, rootfs / "opt" / "comet" / "verified-source")
        marker = rootfs / "etc" / "comet-sglang-queue-revision"
        marker.parent.mkdir(parents=True)
        marker.write_text(SGLANG_0515_UPSTREAM_SHA + "\n")
        redaction_digest = SGLANG_0515_PATCH_DIGESTS[redaction_patch.name]
        redaction_marker = rootfs / "etc" / "comet-sglang-server-args-redaction-revision"
        redaction_marker.write_text(redaction_digest + "\n")
        applied_patch_digests = {
            patch.name: SGLANG_0515_PATCH_DIGESTS[patch.name] for patch in fixture_patches
        }
        manifest = _write_fixture_manifest(
            rootfs,
            name=f"sglang-0515-{variant}",
            upstream_sha=SGLANG_0515_UPSTREAM_SHA,
            patches=applied_patch_digests,
            extra={
                "base_images": allowlist,
                "production_lineages": len(rows),
                "source_manifest_sha256": SGLANG_0515_MANIFEST_DIGEST,
            },
        )
        image = fixture / f"sglang-0515-{variant}-fixture.sqsh"
        _make_squashfs(rootfs, image)
        if (
            _read_squashfs_file(image, "etc/comet-sglang-queue-revision").strip()
            != SGLANG_0515_UPSTREAM_SHA
        ):
            raise ValueError("the SGLang 0.5.15 SquashFS marker is incorrect")
        if (
            _read_squashfs_file(image, "etc/comet-sglang-server-args-redaction-revision").strip()
            != redaction_digest
        ):
            raise ValueError("the SGLang 0.5.15 SquashFS redaction marker is incorrect")
        if _read_squashfs_file(image, "etc/comet-deployed-artifact.json") != manifest:
            raise ValueError("the SGLang 0.5.15 SquashFS manifest is incorrect")
        variant_digests[variant] = _sha256(image)
    return {
        "lineages": str(len(rows)),
        "manifest": "verified",
        "patch_helper": "executed",
        "redaction_variants": ",".join(sorted(redaction_variants)),
        "squashfs": variant_digests["stock"],
        "squashfs_faststart": variant_digests["faststart"],
    }


# Immutable image publisher fixtures.
def _publisher_contract(publisher: Path) -> None:
    """Require immutable publication, collision, revision, and cleanup controls."""
    source = publisher.read_text()
    required = (
        "minimum_bytes=$((250 * 1024 * 1024 * 1024))",
        "status --porcelain=v1 --untracked-files=all",
        "ls-files --error-unmatch",
        "sha256-$image_sha.sqsh",
        '[ -L "$final_image" ]',
        'stat -c "%a" "$final_image"',
        'chmod 0444 "$published_tmp"',
        'mv -T -n -- "$published_tmp" "$final_image"',
        '[[ "${SLURM_JOB_ID:-}" =~ ^[0-9]+$ ]]',
        "mktemp -d",
        "trap cleanup EXIT",
        'rm -rf -- "$local_root"',
        "existing immutable image has the wrong digest",
    )
    if any(token not in source for token in required):
        raise ValueError(f"the publisher contract is incomplete: {publisher.name}")
    subprocess.run(["bash", "-n", str(publisher)], check=True)


def _run_publisher(
    command: list[str],
    *,
    environment: dict[str, str],
    check: bool,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise ValueError(
            "the publisher fixture command failed\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _publisher_command_with_image_dir(command: list[str], image_dir: Path) -> list[str]:
    if len(command) != 6:
        raise ValueError("the publisher fixture command has an invalid argument count")
    return [*command[:4], str(image_dir), command[5]]


def _publisher_local_root_pattern(publisher_name: str) -> str:
    patterns = {
        "build-sglang-faststart-k2p.sbatch": "/tmp/comet-k3q-image-*",
        "build-sglang-faststart-m1.sbatch": "/tmp/comet-k3q-m1-image-*",
    }
    try:
        return patterns[publisher_name]
    except KeyError as error:
        raise ValueError(f"unsupported publisher fixture: {publisher_name}") from error


def validate_publisher_fixture(
    repository_root: Path,
    workspace: Path,
    publisher_name: str,
) -> dict[str, str]:
    """Execute one publisher with an isolated SquashFS fixture.

    Verify inputs, digest naming, mode 0444, reuse, collision safety, and cleanup.
    """
    # Prepare an isolated repository, builder, base image, and Git revision.
    local_root_pattern = _publisher_local_root_pattern(publisher_name)
    original_local_roots = set(Path("/tmp").glob(Path(local_root_pattern).name))
    publisher = repository_root / "scripts" / publisher_name
    _publisher_contract(publisher)
    fixture = workspace / publisher.stem
    repository = fixture / "repository"
    scripts = repository / "scripts"
    images = fixture / "images"
    bin_dir = fixture / "bin"
    scripts.mkdir(parents=True)
    images.mkdir()
    bin_dir.mkdir()
    copied_publisher = scripts / publisher.name
    shutil.copyfile(publisher, copied_publisher)
    copied_publisher.chmod(0o755)
    builder = scripts / "build-sglang-faststart-image.sh"
    builder.write_text(
        f"""#!/bin/bash
set -euo pipefail
case "$1" in */base.sqsh) ;; *) echo "unexpected base" >&2; exit 1 ;; esac
case "$2" in {local_root_pattern}/sglang-kimi-k3-faststart-queue.sqsh) ;;
  *) echo "unexpected output" >&2; exit 1 ;;
esac
cp -- "$1" "$2"
"""
    )
    builder.chmod(0o755)
    fake_df = bin_dir / "df"
    fake_df.write_text(
        """#!/bin/bash
set -euo pipefail
printf 'Avail\n%s\n' "${FAKE_AVAILABLE_BYTES:?}"
"""
    )
    fake_df.chmod(0o755)
    (repository / ".gitignore").write_text("/vendor/sglang/patches/ignored.patch\n")
    redaction_patch = (
        repository / "vendor" / "sglang" / "patches" / "0022-server-args-redaction.patch"
    )
    redaction_patch.parent.mkdir(parents=True)
    redaction_patch.write_text("tracked redaction fixture\n")
    auth_patch = (
        repository
        / "vendor"
        / "sglang"
        / "patches"
        / "0023-authenticate-fast-start-bootstrap.patch"
    )
    auth_patch.write_text("tracked bootstrap-authentication fixture\n")
    base_root = fixture / "base-root"
    base_root.mkdir()
    (base_root / "fixture-marker").write_text("comet issue 523\n")
    redaction_marker = base_root / "etc" / "comet-sglang-server-args-redaction-revision"
    redaction_marker.parent.mkdir()
    redaction_marker.write_text(_sha256(redaction_patch) + "\n")
    auth_marker = base_root / "etc" / "comet-sglang-bootstrap-auth-revision"
    auth_marker.write_text(_sha256(auth_patch) + "\n")
    base = fixture / "base.sqsh"
    _make_squashfs(base_root, base)
    git_commands = (
        ["git", "init", "-q"],
        ["git", "add", "."],
        [
            "git",
            "-c",
            "user.name=Comet CI",
            "-c",
            "user.email=comet-ci@example.invalid",
            "commit",
            "-q",
            "-m",
            "fixture",
        ],
    )
    for git_command in git_commands:
        subprocess.run(git_command, cwd=repository, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    command = [
        "bash",
        str(copied_publisher),
        str(repository),
        str(base),
        str(images),
        revision,
    ]
    minimum = 250 * 1024 * 1024 * 1024
    environment = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SLURM_JOB_ID": f"523{os.getpid()}",
    }

    # Reject unsafe job identifiers and insufficient staging capacity.
    invalid_job = _run_publisher(
        command,
        environment={
            **environment,
            "FAKE_AVAILABLE_BYTES": str(minimum),
            "SLURM_JOB_ID": "../../comet-523",
        },
        check=False,
    )
    if invalid_job.returncode == 0 or "invalid Slurm job ID" not in invalid_job.stderr:
        raise ValueError("the publisher accepted an unsafe Slurm job ID")
    below = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum - 1)},
        check=False,
    )
    if below.returncode == 0 or "requires 250 GiB" not in below.stderr:
        raise ValueError("the publisher did not reject insufficient local capacity")

    # Publish once, verify digest, mode, content, and immutable reuse.
    first = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=True,
    )
    published = list(images.glob("sglang-kimi-k3-faststart-queue-cu12-sha256-*.sqsh"))
    if len(published) != 1:
        raise ValueError("the publisher did not create exactly one immutable image")
    artifact = published[0]
    digest = _sha256(artifact)
    if not artifact.name.endswith(f"sha256-{digest}.sqsh"):
        raise ValueError("the publisher output name does not contain its digest")
    if artifact.stat().st_mode & 0o777 != 0o444:
        raise ValueError("the publisher output mode is not 0444")
    if _read_squashfs_file(artifact, "fixture-marker") != "comet issue 523\n":
        raise ValueError("the published SquashFS cannot be inspected")
    if "published " not in first.stdout:
        raise ValueError("the publisher did not report a new image")
    before_reuse = artifact.stat().st_ino
    second = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum + 1)},
        check=True,
    )
    if "reusing " not in second.stdout or artifact.stat().st_ino != before_reuse:
        raise ValueError("the publisher did not reuse the immutable image")
    if list(images.glob(".sglang-k3q-*")):
        raise ValueError("the publisher retained a temporary image")

    # Reject digest, mode, symlink, and publication-race collisions.
    collision_dir = fixture / "collision-images"
    collision_dir.mkdir()
    collision = collision_dir / artifact.name
    collision.write_bytes(b"incorrect immutable image\n")
    collision.chmod(0o444)
    collision_command = _publisher_command_with_image_dir(command, collision_dir)
    collision_result = _run_publisher(
        collision_command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if (
        collision_result.returncode == 0
        or "existing immutable image has the wrong digest" not in collision_result.stderr
        or collision.read_bytes() != b"incorrect immutable image\n"
    ):
        raise ValueError("the publisher did not preserve a digest collision")

    wrong_mode_dir = fixture / "wrong-mode-images"
    wrong_mode_dir.mkdir()
    wrong_mode = wrong_mode_dir / artifact.name
    shutil.copyfile(artifact, wrong_mode)
    wrong_mode.chmod(0o666)
    wrong_mode_result = _run_publisher(
        _publisher_command_with_image_dir(command, wrong_mode_dir),
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if (
        wrong_mode_result.returncode == 0
        or "not 444" not in wrong_mode_result.stderr
        or wrong_mode.stat().st_mode & 0o777 != 0o666
    ):
        raise ValueError("the publisher accepted a mutable existing image")

    for link_name, target in (
        ("symlink-images", artifact),
        ("dangling-symlink-images", fixture / "missing-image.sqsh"),
    ):
        link_dir = fixture / link_name
        link_dir.mkdir()
        link = link_dir / artifact.name
        link.symlink_to(target)
        link_result = _run_publisher(
            _publisher_command_with_image_dir(command, link_dir),
            environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
            check=False,
        )
        if (
            link_result.returncode == 0
            or "Immutable image path is a symlink" not in link_result.stderr
            or not link.is_symlink()
        ):
            raise ValueError("the publisher accepted an immutable-image symlink")

    race_dir = fixture / "race-images"
    race_dir.mkdir()
    race_final = race_dir / artifact.name
    fake_mv = bin_dir / "mv"
    fake_mv.write_text(
        """#!/bin/bash
set -euo pipefail
if [ -n "${RACE_FINAL:-}" ] && [ ! -e "$RACE_FINAL" ] && [ ! -L "$RACE_FINAL" ]; then
  printf 'racing immutable image\n' > "$RACE_FINAL"
  chmod 0444 "$RACE_FINAL"
fi
exec /usr/bin/mv "$@"
"""
    )
    fake_mv.chmod(0o755)
    race_result = _run_publisher(
        _publisher_command_with_image_dir(command, race_dir),
        environment={
            **environment,
            "FAKE_AVAILABLE_BYTES": str(minimum),
            "RACE_FINAL": str(race_final),
        },
        check=False,
    )
    if (
        race_result.returncode == 0
        or "appeared during publication" not in race_result.stderr
        or race_final.read_bytes() != b"racing immutable image\n"
        or list(race_dir.glob(".sglang-k3q-*"))
    ):
        raise ValueError("the publisher replaced an image created during publication")

    # Reject revision mismatches and dirty repository inputs.
    wrong_revision = _run_publisher(
        [*command[:-1], "0" * 40],
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if wrong_revision.returncode == 0 or "does not match" not in wrong_revision.stderr:
        raise ValueError("the publisher did not reject a different repository revision")

    untracked = repository / "vendor" / "sglang" / "patches" / "untracked.patch"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("untracked fixture\n")
    untracked_result = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if untracked_result.returncode == 0 or "untracked files" not in untracked_result.stderr:
        raise ValueError("the publisher did not reject an untracked build input")
    untracked.unlink()
    ignored = repository / "vendor" / "sglang" / "patches" / "ignored.patch"
    ignored.write_text("ignored fixture\n")
    ignored_result = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if (
        ignored_result.returncode == 0
        or "untracked SGLang patch inputs" not in ignored_result.stderr
    ):
        raise ValueError("the publisher did not reject an ignored build input")
    ignored.unlink()
    builder.write_text(builder.read_text() + "# dirty fixture\n")
    dirty = _run_publisher(
        command,
        environment={**environment, "FAKE_AVAILABLE_BYTES": str(minimum)},
        check=False,
    )
    if dirty.returncode == 0 or "tracked changes" not in dirty.stderr:
        raise ValueError("the publisher did not reject tracked changes")

    # Confirm node-local cleanup and report verified properties.
    retained_local_roots = set(Path("/tmp").glob(Path(local_root_pattern).name))
    if retained_local_roots != original_local_roots:
        raise ValueError("the publisher retained a node-local build directory")
    return {
        "capacity": "verified",
        "local_cleanup": "verified",
        "mode": "0444",
        "reused": "true",
        "sha256": digest,
    }


# JIT seed archive and publisher fixtures.
def _inspect_seed_archive(path: Path) -> tuple[list[tuple[str, str]], dict[str, bytes]]:
    """Return normalized members and bounded file data from a safe tar archive."""
    with tarfile.open(path) as archive:
        members: list[tuple[str, str]] = []
        payloads: dict[str, bytes] = {}
        seen: set[str] = set()
        for member in archive:
            name = member.name
            while name.startswith("./"):
                name = name[2:]
            if name == ".":
                name = ""
            if name.startswith("/") or ".." in name.split("/"):
                raise ValueError(f"the seed archive contains an unsafe path: {member.name}")
            name = name.rstrip("/")
            if name in seen:
                raise ValueError(f"the seed archive contains a duplicate path: {member.name}")
            seen.add(name)
            if member.isdir():
                kind = "directory"
            elif member.isfile():
                kind = "file"
                if member.size > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"the seed archive file is too large: {member.name}")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError(f"the seed archive file cannot be read: {member.name}")
                content = extracted.read(MAX_DOWNLOAD_BYTES + 1)
                if len(content) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"the seed archive file is too large: {member.name}")
                payloads[name] = content
            else:
                raise ValueError(f"the seed archive contains a non-file payload: {member.name}")
            members.append((name, kind))
        return sorted(members), payloads


def _tar_file_members(path: Path) -> list[tuple[str, str]]:
    return _inspect_seed_archive(path)[0]


def _tar_file_payloads(path: Path) -> dict[str, bytes]:
    return _inspect_seed_archive(path)[1]


def _require_fixture_weka_mount(workspace: Path) -> Path:
    source = workspace / "weka"
    try:
        isolated = source.is_dir() and source.samefile("/mnt/weka")
    except OSError:
        isolated = False
    if not isolated:
        raise RuntimeError("the canary fixture requires an isolated /mnt/weka bind")
    return source


def _shell_prefix_through_function(source: str, function_name: str) -> str:
    """Return the reviewed shell source through one complete top-level function."""
    header = re.compile(rf"(?m)^{re.escape(function_name)}\(\) \{{\n").search(source)
    if header is None:
        raise ValueError(f"the reviewed shell function is missing: {function_name}")
    terminator = re.compile(r"(?m)^}[ \t]*(?:\n|\Z)").search(source, header.end())
    if terminator is None:
        raise ValueError(f"the reviewed shell function is incomplete: {function_name}")
    return source[: terminator.end()]


def validate_canary_publisher_fixture(
    repository_root: Path,
    workspace: Path,
) -> dict[str, str]:
    """Execute the reviewed canary publisher in an isolated Weka fixture.

    Verify members, file data, mode 0644, no-overwrite publication, and TERM cleanup.
    """
    # Extract the reviewed publisher and prepare isolated inputs.
    canary = repository_root / "scripts" / "sglang-faststart-canary.sbatch"
    _require_reviewed_programs(repository_root, "scripts/sglang-faststart-canary.sbatch")
    subprocess.run(["bash", "-n", str(canary)], check=True)
    source = canary.read_text()
    prefix = _shell_prefix_through_function(source, "publish_jit_seed")
    required = (
        "FASTSTART_JIT_SEED_OUTPUT must be an absolute path",
        "refusing to overwrite JIT seed output",
        "trap cleanup EXIT",
        "trap on_term TERM",
        'mktemp --tmpdir="$output_dir" ".${output_name}.partial-XXXXXX"',
        'tar cf "$jit_seed_partial" -C "$COMET_JIT_CACHE" .',
        'if ! chmod 0644 -- "$jit_seed_partial"; then',
        'mv -T -n -- "$jit_seed_partial" "$jit_seed_output"',
        "if ! publish_jit_seed; then",
    )
    if any(token not in source for token in required):
        raise ValueError("the canary JIT seed publication contract is incomplete")
    fixture = workspace / "canary"
    fixture.mkdir()
    weka_root = _require_fixture_weka_mount(workspace) / "shrd/comet"
    model_path = weka_root / "models" / "glm-5.2"
    model_path.mkdir(parents=True)
    (model_path / "config.json").write_text("{}\n")
    (weka_root / "faststart-canary").mkdir()
    image_root = fixture / "image-root"
    image_root.mkdir()
    (image_root / "marker").write_text("canary fixture\n")
    image = fixture / "image.sqsh"
    _make_squashfs(image_root, image)
    input_root = fixture / "input-cache"
    (input_root / "nested").mkdir(parents=True)
    (input_root / "kernel one.bin").write_bytes(b"kernel-one\n")
    (input_root / "nested" / "kernel-two.bin").write_bytes(b"kernel-two\n")
    seed = fixture / "input.tar"
    with tarfile.open(seed, "w") as archive:
        for path in sorted(input_root.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(input_root))
    harness = fixture / "publish-canary-prefix.sh"
    harness.write_text(prefix + "\npublish_jit_seed\n")
    harness.chmod(0o700)
    output = fixture / "output" / "jit-seed.tar"

    def run(
        output_path: Path,
        job_id: str,
        *,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                "bash",
                "-c",
                'umask 0077; exec bash "$1"',
                "canary-fixture",
                str(harness),
            ],
            env={
                **os.environ,
                "CANARY_ROLE": "donor",
                "FASTSTART_IMAGE": str(image),
                "FASTSTART_JIT_SEED": str(seed),
                "FASTSTART_JIT_SEED_OUTPUT": str(output_path),
                "SLURM_JOB_ID": job_id,
                **(extra_environment or {}),
            },
            check=False,
            capture_output=True,
            text=True,
        )

    # Reject an unsafe publication path.
    relative = run(Path("relative-seed.tar"), f"523{os.getpid()}0")
    if relative.returncode == 0 or "must be an absolute path" not in relative.stderr:
        raise ValueError("the canary accepted a relative JIT seed output")

    # Publish and verify complete members, file data, and mode 0644.
    first = run(output, f"523{os.getpid()}1")
    if first.returncode != 0 or "JIT_SEED_READY" not in first.stdout:
        raise ValueError(f"the canary JIT seed publisher failed: {first.stderr}")
    expected_members = [
        ("", "directory"),
        ("kernel one.bin", "file"),
        ("nested", "directory"),
        ("nested/kernel-two.bin", "file"),
    ]
    archive_members, archive_payloads = _inspect_seed_archive(output)
    if archive_members != expected_members:
        raise ValueError("the canary JIT seed archive has incorrect members")
    if archive_payloads != {
        "kernel one.bin": b"kernel-one\n",
        "nested/kernel-two.bin": b"kernel-two\n",
    }:
        raise ValueError("the canary JIT seed archive has incorrect file content")
    if output.stat().st_mode & 0o777 != 0o644:
        raise ValueError("the canary JIT seed archive mode is not 0644")
    if list(output.parent.glob(".*.partial-*")):
        raise ValueError("the canary retained a partial JIT seed archive")

    # Prove no-overwrite behavior for regular files and symlinks.
    original = output.read_bytes()
    second = run(output, f"523{os.getpid()}2")
    if second.returncode == 0 or "refusing to overwrite" not in second.stderr:
        raise ValueError("the canary did not reject an existing JIT seed")
    if output.read_bytes() != original:
        raise ValueError("the canary changed an existing JIT seed")
    symlink = fixture / "output" / "linked-seed.tar"
    symlink.symlink_to(output)
    linked = run(symlink, f"523{os.getpid()}3")
    if linked.returncode == 0 or "refusing to overwrite" not in linked.stderr:
        raise ValueError("the canary did not reject a JIT seed symlink")

    # Prove TERM removes an unpublished partial archive.
    term_bin = fixture / "term-bin"
    term_bin.mkdir()
    term_tar = term_bin / "tar"
    term_tar.write_text(
        """#!/bin/bash
set -euo pipefail
if [ "$1" = cf ]; then
  printf 'partial seed\n' > "$2"
  kill -TERM "$PPID"
  exit 0
fi
exec /usr/bin/tar "$@"
"""
    )
    term_tar.chmod(0o755)
    terminated_output = fixture / "output" / "terminated-seed.tar"
    terminated = run(
        terminated_output,
        f"523{os.getpid()}4",
        extra_environment={"PATH": f"{term_bin}:{os.environ['PATH']}"},
    )
    if (
        terminated.returncode != 143
        or terminated_output.exists()
        or terminated_output.is_symlink()
        or list(terminated_output.parent.glob(".terminated-seed.tar.partial-*"))
    ):
        raise ValueError("the canary did not stop and clean up after TERM")
    file_count = sum(kind == "file" for _, kind in expected_members)
    return {
        "archive": "verified",
        "members": str(file_count),
        "mode": "0644",
        "no_overwrite": "true",
    }


def validate_jit_seed_fixture(repository_root: Path, workspace: Path) -> dict[str, str]:
    """Verify that the JIT seed wrapper uses the typed runtime boundary."""
    script = repository_root / "scripts" / "seed_jit_cache.sh"
    subprocess.run(["bash", "-n", str(script)], check=True)
    source = script.read_text()
    required = {
        "model syntax check": '[[ "$model" =~ ^[a-z0-9][a-z0-9._-]*$ ]]',
        "Slurm job ID syntax check": '[[ "$job" =~ ^[0-9]+$ ]]',
        "required context selector": "COMET_DEPLOYMENT_CONTEXT:?select a deployment context",
        "typed operational-context module": "python3 -m comet.operational_context",
        "deployment-context selector forwarding": (
            'context_args+=(--deployment-context "$context_path")'
        ),
        "--operation seed-jit": "--operation seed-jit",
        "--model": '--model "$model"',
        "--job-id": '--job-id "$job"',
    }
    missing = [label for label, token in required.items() if token not in source]
    if missing:
        raise ValueError(
            "the JIT cache seed wrapper is missing required wrapper elements: " + ", ".join(missing)
        )

    fake_bin = workspace / "jit-seed-bin"
    fake_bin.mkdir()
    argument_log = workspace / "jit-seed-arguments"
    python_path_log = workspace / "jit-seed-python-path"
    fake_python = fake_bin / "python3"
    fake_python.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$@" > "$COMET_JIT_ARGUMENT_LOG"
printf '%s' "${PYTHONPATH:-}" > "$COMET_JIT_PYTHON_PATH_LOG"
if [[ -n "${COMET_JIT_FAILURE:-}" ]]; then
  printf '%s\n' "$COMET_JIT_FAILURE" >&2
  exit 23
fi
printf '%s\n' '/verified/cache-seed/fixture-model.tar'
"""
    )
    fake_python.chmod(0o755)
    root = workspace / "seed-root"
    context_path = workspace / "m2-development.yaml"
    job = f"523{os.getpid()}"
    environment = {
        **os.environ,
        "COMET_ROOT": str(root),
        "COMET_JIT_ARGUMENT_LOG": str(argument_log),
        "COMET_JIT_PYTHON_PATH_LOG": str(python_path_log),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }
    command = [
        "bash",
        str(script),
        "fixture-model",
        job,
        "--deployment-context",
        str(context_path),
    ]
    result = subprocess.run(
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            "the JIT cache seed wrapper could not call the typed boundary; "
            f"exit status: {result.returncode}\n"
            f"stdout:\n{_bounded_diagnostic(result.stdout)}\n"
            f"stderr:\n{_bounded_diagnostic(result.stderr)}"
        )
    expected_arguments = [
        "-m",
        "comet.operational_context",
        "--deployment-context",
        str(context_path),
        "--operation",
        "seed-jit",
        "--model",
        "fixture-model",
        "--job-id",
        job,
    ]
    actual_arguments = argument_log.read_text().splitlines()
    if actual_arguments != expected_arguments:
        raise ValueError(
            "the JIT cache seed wrapper supplied incorrect typed-boundary arguments; "
            f"expected {expected_arguments!r}; observed {actual_arguments!r}"
        )
    expected_python_path = str(repository_root / "src")
    actual_python_path = python_path_log.read_text().split(os.pathsep)[0]
    if actual_python_path != expected_python_path:
        raise ValueError(
            "the JIT cache seed wrapper supplied an incorrect module path; "
            f"expected {expected_python_path!r}; observed {actual_python_path!r}"
        )

    diagnostic = (
        "deployment context validation failed: expected m2-development; "
        "observed m2-production; select the registered development context"
    )
    failed = subprocess.run(
        command,
        env={**environment, "COMET_JIT_FAILURE": diagnostic},
        check=False,
        capture_output=True,
        text=True,
    )
    if failed.returncode != 23 or diagnostic not in failed.stderr:
        raise ValueError(
            "the JIT cache seed wrapper did not preserve the typed-boundary error; "
            f"expected exit status 23 and diagnostic {diagnostic!r}; "
            f"observed exit status {failed.returncode}\n"
            f"stdout:\n{_bounded_diagnostic(failed.stdout)}\n"
            f"stderr:\n{_bounded_diagnostic(failed.stderr)}"
        )
    return {
        "argument_transport": "verified",
        "error_detail": "verified",
        "typed_boundary": "verified",
    }


# Validator registration, rootless fixture execution, and dispatch.
FIXTURE_VALIDATORS = {
    "smg-enroot-image": validate_smg_enroot_fixture,
    "sglang-faststart-enroot-image": validate_sglang_faststart_fixture,
    "sglang-faststart-k2p-publisher": lambda root, work: validate_publisher_fixture(
        root, work, "build-sglang-faststart-k2p.sbatch"
    ),
    "sglang-faststart-m1-publisher": lambda root, work: validate_publisher_fixture(
        root, work, "build-sglang-faststart-m1.sbatch"
    ),
    "sglang-faststart-canary-publisher": validate_canary_publisher_fixture,
    "jit-seed-publisher": validate_jit_seed_fixture,
    "sglang-0515-enroot-image": validate_sglang_0515_fixture,
}


def _build_fixture_tools_image(
    context: Path,
    *,
    runner: Any,
) -> str:
    """Build the pinned local tools image for rootless artifact fixtures."""
    containerfile = context / "Containerfile"
    containerfile.write_text(
        f"""FROM {TOOLS_BASE_IMAGE}
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* \\
    && printf '%s\\n' \\
      'Types: deb' \\
      'URIs: https://snapshot.debian.org/archive/debian/{TOOLS_DEBIAN_SNAPSHOT}/' \\
      'Suites: trixie trixie-updates' \\
      'Components: main' \\
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \\
      'Check-Valid-Until: no' \\
      '' \\
      'Types: deb' \\
      'URIs: https://snapshot.debian.org/archive/debian-security/{TOOLS_DEBIAN_SNAPSHOT}/' \\
      'Suites: trixie-security' \\
      'Components: main' \\
      'Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg' \\
      'Check-Valid-Until: no' \\
      > /etc/apt/sources.list.d/debian.sources
RUN apt-get update -y \\
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
      git=\"{TOOLS_GIT_VERSION}\" squashfs-tools=\"{TOOLS_SQUASHFS_VERSION}\" \\
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /mnt/weka
"""
    )
    iid_file = context / "tools.iid"
    runner(
        [
            "podman",
            "build",
            "--pull=always",
            "--format=docker",
            f"--platform={LINUX_PLATFORM}",
            "--iidfile",
            str(iid_file),
            "--file",
            str(containerfile),
            str(context),
        ],
        check=True,
    )
    return read_image_id(iid_file)


def validate_artifact_fixture(
    validator_id: str,
    repository_root: Path,
    *,
    runner: Any = subprocess.run,
    temporary_root: Path | None = None,
    source_archive_fetcher: Any = _download_kimi_source_archive,
    smg_context_preparer: Any = prepare_smg_production_context,
    ubuntu_snapshot_validator: Any = validate_ubuntu_snapshot_packages,
) -> dict[str, str]:
    """Run one fixture in a hardened rootless Podman container.

    Fetch reviewed remote input before the run. Disable the validation network.
    Mount the repository read-only with isolated writable work and Weka paths.
    Final cleanup requests removal of the local fixture image.
    Accept only a final JSON mapping with string values.
    """
    # Resolve the validator and require a rootless host.
    if validator_id not in FIXTURE_VALIDATORS:
        raise ValueError(f"unknown artifact fixture validator: {validator_id}")
    require_rootless_podman(runner=runner)
    image_id: str | None = None
    with _temporary_directory(temporary_root, "comet-artifact-fixture-") as temporary:
        # Prepare isolated writable mounts and fetch reviewed network input.
        fixture = Path(temporary)
        workspace = fixture / "workspace"
        weka = workspace / "weka"
        weka.mkdir(parents=True)
        try:
            if validator_id == "smg-enroot-image":
                smg_context_preparer(
                    repository_root,
                    workspace / "smg-production-inputs",
                    python_minor="3.12",
                )
                ubuntu_snapshot_validator(
                    repository_root / "docker" / "smg-apt-packages-24.04.txt",
                    snapshot=SMG_ENROOT_UBUNTU_SNAPSHOT,
                    suites=("noble", "noble-updates", "noble-security"),
                    workspace=workspace / "smg-enroot-apt",
                    runner=runner,
                )
            if validator_id == "sglang-faststart-enroot-image":
                source_archive_fetcher(workspace / KIMI_SOURCE_ARCHIVE_NAME)

            # Build the local tools image and run the offline hardened fixture.
            image_id = _build_fixture_tools_image(fixture, runner=runner)
            command = [
                "podman",
                "run",
                "--rm",
                f"--platform={LINUX_PLATFORM}",
                "--network=none",
                "--read-only",
                "--userns=keep-id",
                "--cap-drop=all",
                "--security-opt=no-new-privileges",
                "--tmpfs=/tmp:rw,nosuid,nodev,mode=1777",
                "--env",
                "PYTHONDONTWRITEBYTECODE=1",
                "--mount",
                f"type=bind,src={repository_root},target=/repo,ro=true",
                "--mount",
                f"type=bind,src={workspace},target=/work",
                "--mount",
                f"type=bind,src={weka},target=/mnt/weka",
                image_id,
                "python3",
                "/repo/scripts/deployed_artifact_validators.py",
                "fixture",
                "--validator",
                validator_id,
                "--repository-root",
                "/repo",
                "--workspace",
                "/work",
            ]
            try:
                result = runner(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    f"artifact fixture failed: {validator_id}\n"
                    f"stdout:\n{_bounded_diagnostic(error.stdout)}\n"
                    f"stderr:\n{_bounded_diagnostic(error.stderr)}"
                ) from error
        finally:
            # Attempt local image cleanup for success and failure.
            _remove_image(image_id, runner, "podman")

    # Parse only the final JSON mapping with string keys and values.
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        raise ValueError("the artifact fixture did not return a result")
    payload = json.loads(lines[-1])
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
    ):
        raise ValueError("the artifact fixture result is invalid")
    return payload


def run_repository_validator(
    validator_id: str,
    repository_root: Path,
    *,
    runner: Any = subprocess.run,
) -> dict[str, str]:
    """Run one code-owned repository command list."""
    try:
        commands = REPOSITORY_VALIDATOR_COMMANDS[validator_id]
    except KeyError as error:
        raise ValueError(f"unknown repository validator: {validator_id}") from error
    with tempfile.TemporaryDirectory(prefix="comet-actionlint-") as temporary:
        actionlint: Path | None = None
        if validator_id == "workflow-contracts":
            actionlint = _install_actionlint(Path(temporary))
        for command in commands:
            actual = (
                [str(actionlint), *command[1:]] if command[0] == "actionlint" else list(command)
            )
            runner(actual, cwd=repository_root, check=True)
    return {"commands": str(len(commands)), "validator": validator_id}


def run_validator(
    validator_id: str,
    repository_root: Path,
    *,
    container_engine: str,
) -> dict[str, str]:
    """Dispatch one declared validator without a shell command string."""
    try:
        kind = VALIDATOR_KINDS[validator_id]
    except KeyError as error:
        raise ValueError(f"unknown validator: {validator_id}") from error
    if container_engine not in VALID_CONTAINER_ENGINES_BY_KIND[kind]:
        raise ValueError(
            f"validator {validator_id!r} cannot use container engine {container_engine!r}"
        )
    repository_root = repository_root.resolve(strict=True)
    if kind == "repository-contract":
        return run_repository_validator(validator_id, repository_root)
    if kind == "artifact-fixture":
        return validate_artifact_fixture(validator_id, repository_root)
    if validator_id == "control-image":
        return validate_control_image(repository_root, container_engine=container_engine)
    if validator_id == "smg-docker-image":
        return validate_smg_docker_image(repository_root, container_engine=container_engine)
    raise ValueError(f"validator has no implementation: {validator_id}")


# Internal fixture command-line interface.
def _fixture_command(args: argparse.Namespace) -> int:
    if args.repository_root != Path("/repo") or args.workspace != Path("/work"):
        raise RuntimeError("artifact fixtures require the isolated /repo and /work mounts")
    try:
        validator = FIXTURE_VALIDATORS[args.validator]
    except KeyError as error:
        raise ValueError(f"unknown artifact fixture validator: {args.validator}") from error
    result = validator(args.repository_root, args.workspace)
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("fixture")
    fixture.add_argument("--validator", required=True)
    fixture.add_argument("--repository-root", type=Path, required=True)
    fixture.add_argument("--workspace", type=Path, required=True)
    fixture.set_defaults(handler=_fixture_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run an internal rootless fixture command."""
    args = _parser().parse_args(argv)
    args.repository_root = args.repository_root.resolve(strict=True)
    args.workspace = args.workspace.resolve(strict=True)
    return args.handler(args)


def _bounded_diagnostic(value: str | bytes | None) -> str:
    if value is None:
        return ""
    decoded = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
    if len(decoded) <= MAX_ERROR_DIAGNOSTIC_CHARS:
        return decoded
    return decoded[:MAX_ERROR_DIAGNOSTIC_CHARS] + "\n[truncated]"


def _process_error_message(error: subprocess.CalledProcessError) -> str:
    return (
        f"command failed with exit status {error.returncode}: {error.cmd!r}\n"
        f"stdout:\n{_bounded_diagnostic(error.stdout)}\n"
        f"stderr:\n{_bounded_diagnostic(error.stderr)}"
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        sys.stderr.write(f"deployed-artifact validation failed: {_process_error_message(error)}\n")
        raise SystemExit(1) from None
    except (OSError, RuntimeError, ValueError) as error:
        sys.stderr.write(f"deployed-artifact validation failed: {error}\n")
        raise SystemExit(1) from None
