"""Admit a fixed Comet source profile and select its validation commands.

The source adapter must bind each supplied object and the complete path
inventory to an immutable revision. Profile admission does not execute
repository code. The CI reader permits only fixed read-only GitHub endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Literal, NoReturn, cast

from hephaestus.agents.workspace import WorkspaceBinding
from hephaestus.config.child_environments import build_gh_child_env
from hephaestus.utils.helpers import SubprocessOutputLimitExceeded, run_subprocess

from . import comet_profile_7c2772e
from .pipeline.repository_validation import (
    RepositoryValidationCheck,
    RepositoryValidationGap,
    RepositoryValidationInvocation,
    RepositoryValidationPlan,
    RepositoryValidationReceipt,
)
from .remote_git import trusted_gh_executable
from .worktree_snapshot import _controlled_git_env, _run_bounded_git_output

COMET_PROFILE_ID = "comet-d19d3dd-v1"
COMET_PROFILE_DIGEST = "4ea704b6d0e6e21e37cdf8264d90e7d5f9bebb38428568d3281119aacf2787cb"

# These records bind the source inspected for issue #3088.
_CONTROLS: tuple[tuple[str, int, int, str], ...] = (
    (
        ".gitattributes",
        33188,
        115,
        "5a131fa6e6c16945dd1eb1dbc15327f37d2d88e2a5a2ccc179fa98877e98ef53",
    ),
    (
        ".github/workflows/build-image.yml",
        33188,
        2671,
        "6228063a65fea97c2fd0b2fa7fbd52ea3dbad86a3bfa10b2b0698482b4a089d8",
    ),
    (
        ".github/workflows/ci.yml",
        33188,
        13973,
        "dd58fdbbd4c474a79e37dfe61943a0bc304bc414510a2a0e1d131b25068e61e5",
    ),
    (
        ".github/workflows/contracts.yml",
        33188,
        1463,
        "15a346621af79282ea3cc45710880473094b42025622edbb26685570c0c5b967",
    ),
    (
        ".github/workflows/docs-check.yml",
        33188,
        1494,
        "c60633e2091f84f77c6dec0f3a4d8c1de9c4cad16688dafc1dc3aaab5eb9c00c",
    ),
    (
        ".github/workflows/docs.yml",
        33188,
        4631,
        "bf39ffcce95ef80637e599da94b6a4a400f09e0eef0a9b9463d86f88288b897e",
    ),
    (
        ".github/workflows/hosted-scope-qualification.yml",
        33188,
        29817,
        "7f12756f46a55ddb76eb002500b2b1097efe898123cd252daeb09cb4f42013a5",
    ),
    (
        ".github/workflows/nightly.yml",
        33188,
        2982,
        "19d1cf92dd38ea185bb31ae9a8c2c0b339e556320b24ecb9cfa527418dfeb8f7",
    ),
    (
        ".github/workflows/postgres-rootless-validation.yml",
        33188,
        3338,
        "fd8899fb2bb6dec479b84db06ed7ce2ba33624af341e0e77f2b34cc4b99a20d3",
    ),
    (
        ".github/workflows/postgres-tests.yml",
        33188,
        20109,
        "1d6d1393dc21e5c81cc7349e77972d6d4636fcb225b22a68231e282fc4120dff",
    ),
    (
        ".github/workflows/pr-provenance.yml",
        33188,
        865,
        "fbcdd76eb7ca7929fab07547bbcfd3eef36934d0197ea4a97f46371b0a00626c",
    ),
    (
        ".github/workflows/secrets.yml",
        33188,
        653,
        "2f24b66fdf6702ee428bddf98cdb3e63809d34e0d8404976d0e271a2f8bad98c",
    ),
    (
        ".github/workflows/test-profile.yml",
        33188,
        174617,
        "e32b3d9885bda27809fd49f76d5672fc3b20f1a6960d0e8106ed22a3dcc027d1",
    ),
    (".gitignore", 33188, 129, "2298d7a98f3b4af111a89978100944da391af885416d890d846708b6aff8a764"),
    (
        ".gitleaks.toml",
        33188,
        1296,
        "f3418debc7d7fce627ea418ccec8d558dbb820ec3b9b05380e546f1e475e22e7",
    ),
    (
        ".pre-commit-config.yaml",
        33188,
        1054,
        "f16904c2c97994df95afa0b763b2dc24c1d0ccfdd12732eaa8317f30e2fd9ede",
    ),
    (
        ".python-version",
        33188,
        5,
        "7b55f8e67b5623c4bef3fa691288da9437d79d3aba156de48d481db32ac7d16d",
    ),
    (
        "deployment/deployed-inputs.yaml",
        33188,
        9602,
        "1f9084d4a951979036c8f3eb8c7f5f03ee145736adab4942f59ed8d24e93ed71",
    ),
    ("mkdocs.yml", 33188, 4147, "02ddba21a8ad7c0aa34638d48910984b5f76c4b1d8c552650d0d2eaaed33749f"),
    (
        "pyproject.toml",
        33188,
        4527,
        "6f2c39177653ce91e36dbdbe69e1246f2a0257a8c335fa3dbc3e624ba4f88da2",
    ),
    (
        "runtime/k2-config/pyproject.toml",
        33188,
        604,
        "f021bc5c5d374c6947969b23b0038fb71c286f6add864e9cbf659877bd6c385f",
    ),
    (
        "scripts/check-recycling-coverage.py",
        33188,
        2444,
        "eff1c0f35e7b60e9aea87f79510b6e379091835f78cbe2cb829728d58d2b6d3e",
    ),
    (
        "scripts/check_deployed_inputs.py",
        33188,
        35437,
        "cdeb209b19eab5d383776662c46049abdffed751e7f428d89a9dfa30d5909583",
    ),
    (
        "scripts/ci/postgres_test_runtime.py",
        33188,
        1762,
        "3034c609612ef54fc8926f66829b61801154da64977085ef912e6d62169172a5",
    ),
    (
        "scripts/ci/run-gitleaks.sh",
        33261,
        1282,
        "a0bdbdeec6a43e6c5c5fe1dea9272a51ea482d761c2b43e04f0077aea69cff5b",
    ),
    (
        "scripts/ci/run-test-tier.py",
        33188,
        10052,
        "f18964418c2106a397dd664f8b33299016102123ea63f1786a5bfb4e528e996b",
    ),
    (
        "scripts/ci/summarize-test-durations.py",
        33188,
        3185,
        "e7d70e746a7bc7f9e8d3269e7f3d523159fd9104dc99fec40ff71bdf72ea3df1",
    ),
    (
        "scripts/configure-test-postgres-tls.sh",
        33261,
        4728,
        "76981b93c1fd4b2312ac1a5730ded470a4bc2a594a1126eeeac2d91cb5f3392c",
    ),
    (
        "scripts/deployed_artifact_validators.py",
        33188,
        112865,
        "b9fe94001aae5a279c4ef965ecb9d39db947d76df4e357971c21bf2e3085e14f",
    ),
    (
        "scripts/diagnose-test-postgres.sh",
        33261,
        4132,
        "832844bc5cf5166c808e5a25d6dc61831eb48912911a302bff8a7581d5ae2350",
    ),
    (
        "scripts/smg_wheelhouse.py",
        33188,
        10612,
        "2451ffbd103cc94b0b89592f5d40c02450e63a2f3460f3e859e2c813e8092962",
    ),
    (
        "scripts/validate-pr-policy.sh",
        33261,
        7469,
        "a97e6d553bc1660d82d4d48ce4a1fb3cd104586b2b9d79994def1670604fb73d",
    ),
    (
        "tests/__init__.py",
        33188,
        34,
        "c47ef25cccfe8a923c3128211b40f2b5374ca6bd1da337cf40dc576299556065",
    ),
    (
        "tests/conftest.py",
        33188,
        2108,
        "be099d4741ccba74ffb721f59a914a8f46cba77264f112cf1228bb5aee22c8b7",
    ),
    (
        "tests/deployment_context_helpers.py",
        33188,
        12088,
        "796dcc1542f14e102e9b42bc4e2e67675d76c692d98960bbe7d9cea6f24e77a7",
    ),
    (
        "tests/release_effect_guard.py",
        33188,
        3494,
        "d55dcd37e09092c4822d58b719792a54b26ba5a34e2ca14b694e60277ac0bc1d",
    ),
    (
        "tests/test-tiers.yaml",
        33188,
        7489,
        "6d823fcf2a30d5feadc61cd303f4f78a23baac79a0fe4a5ed90adba51fa9870e",
    ),
    ("uv.lock", 33188, 121280, "df261ef8e5ddca3317594213e12140b4fbfb750cb09298006e22a530c9c97da2"),
)

_SELECTION: dict[str, Any] = {
    "candidates": {
        "exclude": [
            "docker/k2-sglang/**",
            "docker/vllm-k2/**",
            "docker/sglang-faststart.Dockerfile",
            "ops/grafana/README.md",
            "scripts/ci/**",
            "scripts/check_deployed_inputs.py",
            "scripts/check_deployment_context_inventory.py",
            "scripts/check-recycling-coverage.py",
            "scripts/build_vllm_k2_wheel.py",
            "scripts/configure-test-postgres-tls.sh",
            "scripts/criu_spike.sbatch",
            "scripts/criu_spike2.sbatch",
            "scripts/deployed_artifact_validators.py",
            "scripts/diagnose-test-postgres.sh",
            "scripts/derisk_smg.sbatch",
            "scripts/dev/**",
            "scripts/export_schemas.py",
            "scripts/generate-postgres-release-fixture.py",
            "scripts/build_k2_sglang_image.py",
            "scripts/k3_canary_response_probe.py",
            "scripts/k3_canary_routing_evidence.py",
            "scripts/loadtest.py",
            "scripts/m1_k3_verify_staged_artifacts.py",
            "scripts/nccl_checkpoint_probe.py",
            "scripts/nccl_checkpoint_probe.sbatch",
            "scripts/prepare-sglang-faststart.sh",
            "scripts/probe_k2_sglang_image.py",
            "scripts/preflight_vllm_k2_wheel.py",
            "scripts/validate-pr-policy.sh",
            "scripts/validate_vllm_k2_inputs.py",
            "vendor/sglang/README.md",
            "vendor/sglang-0515/README.md",
            "vendor/sglang-glm53/README.md",
        ],
        "files": [
            ".containerignore",
            ".dockerignore",
            ".gitattributes",
            "README.md",
            "deployment/control-build-constraints.in",
            "deployment/control-build-constraints.txt",
            "pyproject.toml",
            "schema/deployment-context.schema.json",
            "schema/source-admission.schema.json",
            "uv.lock",
            "uv.toml",
        ],
        "prefixes": [
            "clusters/",
            "cookbooks/",
            "deployment/contexts/",
            "deployment/k2-horizon/",
            "deployment/source-admissions/",
            "docker/",
            "migrations/",
            "ops/grafana/",
            "ops/postgres/",
            "scripts/",
            "src/",
            "vendor/sglang/",
            "vendor/sglang-0515/",
            "vendor/sglang-glm53/",
        ],
    },
    "ci_policies": (
        {
            "exclude": (),
            "id": "ci-bootstrap",
            "include": (
                "deployment/deployed-inputs.yaml",
                "scripts/check_deployed_inputs.py",
                "scripts/deployed_artifact_validators.py",
                "tests/test_deployed_artifacts.py",
                "tests/test_deployed_inputs.py",
            ),
            "validators": (
                "schema-contracts",
                "control-deployment-contracts",
                "viewer-deployment-contracts",
                "workflow-contracts",
                "deployed-input-contracts",
                "sglang-k2-contracts",
                "sglang-glm53-image-contracts",
            ),
        },
        {
            "exclude": (),
            "id": "sglang-k2-contracts",
            "include": (
                "docker/k2-sglang/**",
                "docs/k2-sglang-base-image.md",
                "scripts/build_k2_sglang_image.py",
                "scripts/probe_k2_sglang_image.py",
                "tests/test_k2_sglang_image.py",
            ),
            "validators": ("sglang-k2-contracts",),
        },
        {
            "exclude": ("vendor/sglang-glm53/README.md",),
            "id": "sglang-glm53-image-contracts",
            "include": (
                "scripts/build-sglang-glm53-image.sh",
                "tests/test_glm53_image.py",
                "tests/test_runtime_artifact.py",
                "tests/test_sglang_redaction_images.py",
                "vendor/sglang-0515/apply_queue_patch.py",
                "vendor/sglang-glm53/**",
            ),
            "validators": ("sglang-glm53-image-contracts",),
        },
        {
            "exclude": (),
            "id": "vllm-k2-contracts",
            "include": (
                "docker/vllm-k2/**",
                "docs/vllm-k2-wheel.md",
                "scripts/validate_vllm_k2_inputs.py",
                "scripts/build_vllm_k2_wheel.py",
                "scripts/preflight_vllm_k2_wheel.py",
                "tests/test_vllm_k2_inputs.py",
                "tests/test_vllm_k2_wheel.py",
                "tests/test_vllm_k2_wheel_preflight.py",
            ),
            "validators": ("deployed-input-contracts",),
        },
        {
            "exclude": (),
            "id": "workflow-contracts",
            "include": (
                ".github/actions/**",
                ".github/workflows/**",
                ".gitleaks.toml",
                ".gitleaksignore",
                "scripts/check-recycling-coverage.py",
                "scripts/validate-pr-policy.sh",
                "scripts/ci/**",
                "tests/test_gitleaks_policy.py",
                "tests/test_ci_workflows.py",
            ),
            "validators": ("workflow-contracts",),
        },
        {
            "exclude": (),
            "id": "schema-contracts",
            "include": (
                "schema/**",
                "scripts/export_schemas.py",
                "src/comet/image_probe.py",
                "tests/test_config.py",
                "tests/test_image_probe_contract.py",
                "tests/test_schema_export.py",
            ),
            "validators": ("schema-contracts",),
        },
        {
            "exclude": (),
            "id": "control-deployment-contracts",
            "include": ("tests/test_atomic_deploy.py",),
            "validators": ("control-deployment-contracts",),
        },
        {
            "exclude": (),
            "id": "viewer-deployment-contracts",
            "include": (
                "tests/test_grafana_provisioning.py",
                "tests/test_ingest.py",
                "tests/test_secondary_dashboard_rollups.py",
                "tests/test_telemetry_rollup_backfill.py",
            ),
            "validators": ("viewer-deployment-contracts",),
        },
    ),
    "contracts": {
        "control-deployment-contracts": "nightly",
        "deployed-input-contracts": "pr",
        "schema-contracts": "pr",
        "sglang-glm53-image-contracts": "pr",
        "sglang-k2-contracts": "pr",
        "viewer-deployment-contracts": "nightly",
        "workflow-contracts": "pr",
    },
    "forbidden": (".gitmodules", "docker/sglang-faststart.Dockerfile"),
    "policies": [
        {
            "component": "control-plane",
            "id": "control-image-context",
            "include": [".containerignore", ".dockerignore"],
            "validators": ["control-image"],
        },
        {
            "component": "shared-deployment",
            "id": "repository-archive",
            "include": [".gitattributes"],
            "validators": ["control-deployment-contracts", "viewer-deployment-contracts"],
        },
        {
            "component": "control-plane",
            "exclude": [
                "src/comet/operational_context.py",
                "src/comet/k2_horizon.py",
                "src/comet/k2_source_admission.py",
                "src/comet/source_admission.py",
            ],
            "id": "control-package",
            "include": ["README.md", "pyproject.toml", "src/**"],
            "validators": [
                "control-image",
                "control-deployment-contracts",
                "viewer-deployment-contracts",
            ],
        },
        {
            "component": "control-plane",
            "id": "cluster-runtime-data",
            "include": ["clusters/**"],
            "validators": ["control-image", "control-deployment-contracts"],
        },
        {
            "component": "control-plane",
            "id": "cookbook-runtime-data",
            "include": ["cookbooks/**"],
            "validators": ["control-image", "control-deployment-contracts"],
        },
        {
            "component": "control-plane",
            "id": "deployment-context-runtime-data",
            "include": ["deployment/contexts/**"],
            "validators": ["control-image", "schema-contracts", "control-deployment-contracts"],
        },
        {
            "component": "control-plane",
            "id": "k2-horizon-runtime-data",
            "include": [
                "deployment/k2-horizon/**",
                "src/comet/k2_horizon.py",
                "src/comet/k2_source_admission.py",
            ],
            "validators": ["control-image"],
        },
        {
            "component": "control-plane",
            "id": "source-admission-runtime-data",
            "include": [
                "deployment/source-admissions/**",
                "schema/source-admission.schema.json",
                "src/comet/source_admission.py",
            ],
            "validators": ["control-image", "schema-contracts"],
        },
        {
            "component": "control-plane",
            "id": "deployment-context-schema",
            "include": ["schema/deployment-context.schema.json"],
            "validators": ["control-image", "schema-contracts"],
        },
        {
            "component": "shared-runtime",
            "id": "database-migrations",
            "include": ["migrations/**"],
            "validators": [
                "control-image",
                "control-deployment-contracts",
                "viewer-deployment-contracts",
            ],
        },
        {
            "component": "shared-runtime",
            "id": "dependency-lock",
            "include": ["uv.lock"],
            "validators": [
                "control-image",
                "control-deployment-contracts",
                "viewer-deployment-contracts",
            ],
        },
        {
            "component": "shared-runtime",
            "forbidden": True,
            "id": "uv-project-config",
            "include": ["uv.toml"],
            "validators": [],
        },
        {
            "component": "control-plane",
            "id": "control-image",
            "include": ["docker/Dockerfile"],
            "validators": ["control-image"],
        },
        {
            "component": "smg",
            "id": "smg-docker",
            "include": ["docker/smg.Dockerfile"],
            "validators": ["smg-docker-image"],
        },
        {
            "component": "smg",
            "id": "smg-build-inputs",
            "include": [
                "docker/smg-apt-packages-24.04.txt",
                "docker/smg-apt-packages-26.04.txt",
                "docker/smg-python-wheels.lock.json",
                "scripts/smg_wheelhouse.py",
                "scripts/verify-smg-wheel-metadata.py",
            ],
            "validators": ["smg-docker-image", "smg-enroot-image"],
        },
        {
            "component": "control-plane",
            "id": "control-deployment",
            "include": [
                "deployment/control-build-constraints.in",
                "deployment/control-build-constraints.txt",
                "scripts/hot-swap-gpu-monitors.sh",
                "scripts/install-control-venv.sh",
                "scripts/repair-core-permissions.py",
                "scripts/probe_slurm_identity.py",
                "scripts/pull-to-cluster.sh",
                "scripts/chaos_pass.py",
                "scripts/setup-engine-venvs.sh",
            ],
            "validators": ["control-deployment-contracts"],
        },
        {
            "component": "smg",
            "id": "smg-enroot",
            "include": ["scripts/build-smg-image.sh"],
            "validators": ["smg-enroot-image"],
        },
        {
            "component": "sglang-faststart",
            "id": "sglang-faststart",
            "include": [
                "scripts/build-sglang-faststart-image.sh",
                "vendor/sglang/UPSTREAM.env",
                "vendor/sglang/patches/**",
            ],
            "validators": [
                "sglang-faststart-enroot-image",
                "sglang-faststart-k2p-publisher",
                "sglang-faststart-m1-publisher",
            ],
        },
        {
            "component": "sglang-faststart",
            "id": "sglang-faststart-k2p-publisher",
            "include": ["scripts/build-sglang-faststart-k2p.sbatch"],
            "validators": ["sglang-faststart-k2p-publisher"],
        },
        {
            "component": "sglang-faststart",
            "id": "sglang-faststart-m1-publisher",
            "include": ["scripts/build-sglang-faststart-m1.sbatch"],
            "validators": ["sglang-faststart-m1-publisher"],
        },
        {
            "component": "sglang-canary",
            "id": "sglang-faststart-canary-publisher",
            "include": ["scripts/sglang-faststart-canary.sbatch"],
            "validators": ["sglang-faststart-canary-publisher"],
        },
        {
            "component": "runtime-cache",
            "id": "jit-seed-publisher",
            "include": ["scripts/seed_jit_cache.sh", "src/comet/operational_context.py"],
            "validators": [
                "jit-seed-publisher",
                "control-image",
                "control-deployment-contracts",
                "viewer-deployment-contracts",
            ],
        },
        {
            "component": "sglang-0515",
            "id": "sglang-0515",
            "include": [
                "scripts/build-sglang-0515-queue-image.sh",
                "vendor/sglang-0515/SOURCE_SHA256",
                "vendor/sglang-0515/UPSTREAM.env",
                "vendor/sglang-0515/apply_queue_patch.py",
                "vendor/sglang-0515/patches/**",
            ],
            "validators": ["sglang-0515-enroot-image"],
        },
        {
            "component": "sglang-glm53",
            "id": "sglang-glm53",
            "include": [
                "scripts/build-sglang-glm53-image.sh",
                "vendor/sglang-glm53/SOURCE_SHA256",
                "vendor/sglang-glm53/UPSTREAM.env",
                "vendor/sglang-glm53/patches/**",
            ],
            "validators": ["sglang-glm53-image-contracts"],
        },
        {
            "component": "viewer",
            "id": "viewer-config",
            "include": [
                "ops/grafana/dashboards/**",
                "ops/grafana/grafana.ini",
                "ops/grafana/provision-comet-grafana-ro.sql",
                "ops/grafana/provisioning/**",
                "ops/grafana/refresh-comet-grafana-grants.sql",
                "ops/grafana/systemd/**",
            ],
            "validators": ["viewer-deployment-contracts"],
        },
        {
            "component": "viewer",
            "id": "viewer-deployment",
            "include": [
                "ops/postgres/rds-global-bundle.pem",
                "ops/postgres/rds-global-bundle.sha256",
                "scripts/backfill-secondary-dashboard-rollups.py",
                "scripts/backfill-telemetry-rollups.py",
                "scripts/psql-with-comet-dsn.sh",
                "scripts/pull-to-viewer.sh",
            ],
            "validators": ["viewer-deployment-contracts"],
        },
    ],
    "retired": ("CHANGELOG.md", "changes/**", "schema/change-fragment.schema.json"),
}

# This second profile binds the retained run for PR #1719. Its policy is
# the current finite policy without the later GLM53 additions.
_HISTORICAL_CONTROL_CHANGES = {
    "deployment/deployed-inputs.yaml": (
        33188,
        9139,
        "ef4ce03a2cd7a3a8cb96db2d5813b1c21a28e48fb0d52bba97a18be3e46c5db1",
    ),
    "scripts/check_deployed_inputs.py": (
        33188,
        34914,
        "1299899fa31c7f8f6fff6a5c4eb1dbe188553ed98341b8aaadba321f7efcba1f",
    ),
    "scripts/deployed_artifact_validators.py": (
        33188,
        112457,
        "4ba97a989e158fe7b95c9b4d22fcb3bb987d467d73260562d36fad9f8a31b0d2",
    ),
    "tests/test-tiers.yaml": (
        33188,
        7459,
        "6e402e1ab1668049ba38407c7a8c4bde4c92020c8ad13a9f49211f7b839ffd26",
    ),
}
_HISTORICAL_CONTROLS = tuple(
    (path, *_HISTORICAL_CONTROL_CHANGES.get(path, (mode, size, digest)))
    for path, mode, size, digest in _CONTROLS
)
_HISTORICAL_SELECTION = {
    **_SELECTION,
    "candidates": {
        **_SELECTION["candidates"],
        "prefixes": [
            p for p in _SELECTION["candidates"]["prefixes"] if p != "vendor/sglang-glm53/"
        ],
        "exclude": [
            p for p in _SELECTION["candidates"]["exclude"] if p != "vendor/sglang-glm53/README.md"
        ],
    },
    "policies": [p for p in _SELECTION["policies"] if p["id"] != "sglang-glm53"],
    "contracts": {
        k: v for k, v in _SELECTION["contracts"].items() if k != "sglang-glm53-image-contracts"
    },
    "ci_policies": tuple(
        {
            **p,
            "validators": tuple(v for v in p["validators"] if v != "sglang-glm53-image-contracts"),
        }
        for p in _SELECTION["ci_policies"]
        if p["id"] != "sglang-glm53-image-contracts"
    ),
}
# This profile binds the public-root test controls and support-script policy.
_FE5A67D_CONTROL_CHANGES = {
    "deployment/deployed-inputs.yaml": (
        33188,
        9653,
        "ea21437f33f5f850eb3b3eae2da48f0d2de5fa4e93774be9826541485f84f0ec",
    ),
    "tests/deployment_context_helpers.py": (
        33188,
        13420,
        "6d3e00f4e53908810d9c6422597824d132db9f5a63072869da7cbca36b8b1bbc",
    ),
    "tests/test-tiers.yaml": (
        33188,
        7855,
        "116d5703e9363e3603d1ee90cc50991ed43292cf9291aa1968d605dc6022203f",
    ),
}
_FE5A67D_CONTROLS = tuple(
    (path, *_FE5A67D_CONTROL_CHANGES.get(path, (mode, size, digest)))
    for path, mode, size, digest in _CONTROLS
)
_FE5A67D_SELECTION = {
    **_SELECTION,
    "policies": [
        {
            **policy,
            "include": [
                "deployment/control-build-constraints.in",
                "deployment/control-build-constraints.txt",
                "scripts/build_public_access_application.py",
                "scripts/hot-swap-gpu-monitors.sh",
                "scripts/install-control-venv.sh",
                "scripts/repair-core-permissions.py",
                "scripts/probe_slurm_identity.py",
                "scripts/pull-to-cluster.sh",
                "scripts/chaos_pass.py",
                "scripts/setup-engine-venvs.sh",
            ],
        }
        if policy["id"] == "control-deployment"
        else policy
        for policy in _SELECTION["policies"]
    ],
}
# Profile digests cover canonical JSON controls and selection.
_PROFILES = {
    comet_profile_7c2772e.PROFILE_ID: (
        comet_profile_7c2772e.PROFILE_DIGEST,
        comet_profile_7c2772e.CONTROLS,
        comet_profile_7c2772e.SELECTION,
    ),
    "comet-fe5a67d-v1": (
        "e225e80e95bcdb376e599b6cd3d735ecebfef6f65b1de2fdfcecc8e09878aed2",
        _FE5A67D_CONTROLS,
        _FE5A67D_SELECTION,
    ),
    COMET_PROFILE_ID: (COMET_PROFILE_DIGEST, _CONTROLS, _SELECTION),
    "comet-5232ef5-v1": (
        "3fec7b1cdfc51c16e8fc89029cf8df857ebfe3927753b5f2a794d93c16aa7c21",
        _HISTORICAL_CONTROLS,
        _HISTORICAL_SELECTION,
    ),
}
_BASE_CONTROL_PATHS = frozenset(row[0] for row in _CONTROLS)
_CONTROL_PATHS = frozenset(row[0] for _, controls, _ in _PROFILES.values() for row in controls)
_CONTROL_MODES = {
    path: frozenset(
        row[1] for _, controls, _ in _PROFILES.values() for row in controls if row[0] == path
    )
    for path in _CONTROL_PATHS
}
_CONTROL_SIZES = {
    path: frozenset(
        row[2] for _, controls, _ in _PROFILES.values() for row in controls if row[0] == path
    )
    for path in _CONTROL_PATHS
}


def comet_profile_digest(profile: str) -> str:
    """Return the identity of one explicitly admitted source profile."""
    if profile not in _PROFILES:
        raise ValueError("The Comet validation profile is unsupported.")
    return _PROFILES[profile][0]


_CONFIG_NAMES = frozenset(
    {
        "pyproject.toml",
        "uv.toml",
        "ruff.toml",
        ".ruff.toml",
        "ty.toml",
        "pytest.ini",
        ".pytest.ini",
        "tox.ini",
        "setup.cfg",
        "conftest.py",
        ".gitignore",
        ".ignore",
        ".gitmodules",
        ".gitattributes",
        ".python-version",
        "action.yml",
        "action.yaml",
    }
)
# The inventory uses Python 3.12 and the recorded Linux dependency closure.
# Execution adapters must prove coverage for their own marker environment.
_TOOL_IMPORTS = frozenset(
    (
        "__future__",
        "_abc",
        "_aix_support",
        "_ast",
        "_asyncio",
        "_bisect",
        "_blake2",
        "_bz2",
        "_codecs",
        "_codecs_cn",
        "_codecs_hk",
        "_codecs_iso2022",
        "_codecs_jp",
        "_codecs_kr",
        "_codecs_tw",
        "_collections",
        "_collections_abc",
        "_compat_pickle",
        "_compression",
        "_contextvars",
        "_crypt",
        "_csv",
        "_ctypes",
        "_curses",
        "_curses_panel",
        "_datetime",
        "_dbm",
        "_decimal",
        "_elementtree",
        "_frozen_importlib",
        "_frozen_importlib_external",
        "_functools",
        "_gdbm",
        "_hashlib",
        "_heapq",
        "_imp",
        "_io",
        "_json",
        "_locale",
        "_lsprof",
        "_lzma",
        "_markupbase",
        "_md5",
        "_msi",
        "_multibytecodec",
        "_multiprocessing",
        "_opcode",
        "_operator",
        "_osx_support",
        "_overlapped",
        "_pickle",
        "_posixshmem",
        "_posixsubprocess",
        "_py_abc",
        "_pydatetime",
        "_pydecimal",
        "_pyio",
        "_pylong",
        "_pytest",
        "_queue",
        "_random",
        "_scproxy",
        "_sha1",
        "_sha2",
        "_sha3",
        "_signal",
        "_sitebuiltins",
        "_socket",
        "_sqlite3",
        "_sre",
        "_ssl",
        "_stat",
        "_statistics",
        "_string",
        "_strptime",
        "_struct",
        "_symtable",
        "_thread",
        "_threading_local",
        "_tkinter",
        "_tokenize",
        "_tracemalloc",
        "_typing",
        "_uuid",
        "_warnings",
        "_weakref",
        "_weakrefset",
        "_winapi",
        "_yaml",
        "_zoneinfo",
        "abc",
        "aifc",
        "annotated_doc",
        "annotated_types",
        "antigravity",
        "anyio",
        "argparse",
        "array",
        "ast",
        "asyncio",
        "asyncpg",
        "atexit",
        "attr",
        "attrs",
        "audioop",
        "babel",
        "backrefs",
        "base64",
        "bdb",
        "binascii",
        "bisect",
        "builtins",
        "bz2",
        "cProfile",
        "calendar",
        "certifi",
        "cfgv",
        "cgi",
        "cgitb",
        "charset_normalizer",
        "chunk",
        "click",
        "cmath",
        "cmd",
        "code",
        "codecs",
        "codeop",
        "collections",
        "colorama",
        "colorsys",
        "compileall",
        "concurrent",
        "configparser",
        "contextlib",
        "contextvars",
        "copy",
        "copyreg",
        "coverage",
        "crypt",
        "csv",
        "ctypes",
        "curses",
        "dataclasses",
        "datetime",
        "dateutil",
        "dbm",
        "decimal",
        "deployed_artifact_validators",
        "difflib",
        "dis",
        "distlib",
        "doctest",
        "email",
        "encodings",
        "ensurepip",
        "enum",
        "errno",
        "fastapi",
        "faulthandler",
        "fcntl",
        "filecmp",
        "fileinput",
        "filelock",
        "fnmatch",
        "fractions",
        "fsspec",
        "ftplib",
        "functools",
        "gc",
        "genericpath",
        "getopt",
        "getpass",
        "gettext",
        "ghp_import",
        "glob",
        "google",
        "graphlib",
        "grp",
        "grpc",
        "grpc_health",
        "gzip",
        "h11",
        "hashlib",
        "heapq",
        "hf_xet",
        "hmac",
        "html",
        "http",
        "httpcore",
        "httpx",
        "huggingface_hub",
        "identify",
        "idlelib",
        "idna",
        "images",
        "imaplib",
        "imghdr",
        "importlib",
        "iniconfig",
        "inspect",
        "io",
        "ipaddress",
        "itertools",
        "jinja2",
        "json",
        "jsonschema",
        "jsonschema_specifications",
        "keyword",
        "k2_ci_policy",
        "lib2to3",
        "linecache",
        "locale",
        "logging",
        "lzma",
        "mailbox",
        "mailcap",
        "markdown",
        "markdown_it",
        "markupsafe",
        "marshal",
        "material",
        "materialx",
        "math",
        "mdurl",
        "mergedeep",
        "mimetypes",
        "mkdocs",
        "mkdocs_get_deps",
        "mmap",
        "modulefinder",
        "msilib",
        "msvcrt",
        "multiprocessing",
        "netrc",
        "nis",
        "nntplib",
        "nodeenv",
        "nt",
        "ntpath",
        "nturl2path",
        "numbers",
        "opcode",
        "operator",
        "optparse",
        "os",
        "ossaudiodev",
        "packaging",
        "paginate",
        "pathlib",
        "pathspec",
        "pdb",
        "pickle",
        "pickletools",
        "pipes",
        "pkgutil",
        "platform",
        "platformdirs",
        "plistlib",
        "pluggy",
        "poplib",
        "posix",
        "posixpath",
        "pprint",
        "pre_commit",
        "profile",
        "pstats",
        "pty",
        "pwd",
        "py",
        "py_compile",
        "pyclbr",
        "pydantic",
        "pydantic_core",
        "pydoc",
        "pydoc_data",
        "pyexpat",
        "pygments",
        "pymdownx",
        "pytest",
        "pytest_asyncio",
        "pytest_cov",
        "python_discovery",
        "queue",
        "quopri",
        "random",
        "re",
        "readline",
        "referencing",
        "reprlib",
        "requests",
        "resource",
        "rich",
        "rlcompleter",
        "rpds",
        "ruff",
        "runpy",
        "sched",
        "secrets",
        "select",
        "selectors",
        "shellingham",
        "shelve",
        "shlex",
        "shutil",
        "signal",
        "site",
        "sitecustomize",
        "six",
        "smg_wheelhouse",
        "smtplib",
        "sndhdr",
        "socket",
        "socketserver",
        "spwd",
        "sqlite3",
        "sre_compile",
        "sre_constants",
        "sre_parse",
        "ssl",
        "starlette",
        "stat",
        "statistics",
        "string",
        "stringprep",
        "struct",
        "subprocess",
        "sunau",
        "symtable",
        "sys",
        "sysconfig",
        "syslog",
        "tabnanny",
        "tarfile",
        "telnetlib",
        "tempfile",
        "termios",
        "textwrap",
        "this",
        "threading",
        "time",
        "timeit",
        "tkinter",
        "token",
        "tokenize",
        "tomllib",
        "tqdm",
        "trace",
        "traceback",
        "tracemalloc",
        "tty",
        "turtle",
        "turtledemo",
        "ty",
        "typer",
        "types",
        "typing",
        "typing_extensions",
        "typing_inspection",
        "unicodedata",
        "unittest",
        "urllib",
        "urllib3",
        "usercustomize",
        "uu",
        "uuid",
        "uvicorn",
        "uvloop",
        "venv",
        "virtualenv",
        "warnings",
        "watchdog",
        "wave",
        "weakref",
        "webbrowser",
        "winreg",
        "winsound",
        "wsgiref",
        "xdrlib",
        "xml",
        "xmlrpc",
        "yaml",
        "yaml_env_tag",
        "zipapp",
        "zipfile",
        "zipimport",
        "zlib",
        "zoneinfo",
    )
)
_PREFIX = ("uv", "run", "--locked", "--extra", "dev")
_PYTHON_COMMANDS = (
    ("comet.python.ruff-format", (*_PREFIX, "ruff", "format", "--check", ".")),
    ("comet.python.ruff-check", (*_PREFIX, "ruff", "check", ".")),
    ("comet.python.ty-check", (*_PREFIX, "ty", "check")),
    ("comet.python.pr-tests", (*_PREFIX, "python", "scripts/ci/run-test-tier.py", "pr")),
)


def _python_commands(profile: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if profile == comet_profile_7c2772e.PROFILE_ID:
        first, *remaining = _PYTHON_COMMANDS
        return ((first[0], (*first[1], "--diff")), *remaining)
    return _PYTHON_COMMANDS


def _valid_path(value: object) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 1024
        and "\0" not in value
        and "\\" not in value
        and ":" not in value
        and not PurePosixPath(value).is_absolute()
        and all(part not in {"", ".", "..", ".git"} for part in value.split("/"))
    )


def _new_control(path: str) -> bool:
    if path in _CONTROL_PATHS:
        return False
    # The admitted commands do not collect PostgreSQL tests. Keep its
    # existing fixture boundary separate from root test collection.
    if path == "tests/postgres/conftest.py":
        return False
    parts = PurePosixPath(path).parts
    if parts[-1] in _CONFIG_NAMES or path.endswith(".pth"):
        return True
    if path.startswith((".github/actions/", ".github/workflows/")):
        return True
    if path.startswith("tests/") and parts[-1] == "__init__.py":
        return not path.startswith("tests/postgres/")
    # Source root, src, and scripts participate in the admitted import
    # paths. A new module there cannot replace a validation dependency.
    for prefix in ((), ("src",), ("scripts",), ("scripts", "ci")):
        if parts[: len(prefix)] != prefix or len(parts) <= len(prefix):
            continue
        candidate = parts[len(prefix)].split(".", 1)[0]
        if candidate in _TOOL_IMPORTS:
            return True
    return False


def admit_comet_controls(
    controls: Mapping[str, tuple[int, bytes]], inventory: tuple[str, ...]
) -> str:
    """Match complete, revision-bound inputs to an explicitly reviewed profile.

    The caller supplies immutable Git file modes and bytes, not data read
    through worktree symlinks. It must prove inventory completeness first.
    """
    if type(inventory) is not tuple or len(inventory) > 500_000:
        raise ValueError("The control path inventory exceeds its limit.")
    seen: set[str] = set()
    inventory_bytes = 0
    for path in inventory:
        if not _valid_path(path) or path in seen:
            raise ValueError("The control path inventory is invalid.")
        seen.add(path)
        inventory_bytes += len(path.encode("utf-8")) + 1
        if inventory_bytes > 64 * 1024 * 1024 or _new_control(path):
            raise ValueError("The control path inventory is unsupported.")
    if set(controls) != seen & _CONTROL_PATHS or not seen >= _BASE_CONTROL_PATHS:
        raise ValueError("The control inventory does not match the profile.")
    total = 0
    actual = []
    for path in sorted(controls):
        record = controls[path]
        if type(record) is not tuple or len(record) != 2:
            raise ValueError("The control record is invalid.")
        mode, data = record
        if type(mode) is not int or type(data) is not bytes or len(data) > 16 * 1024 * 1024:
            raise ValueError("The control file does not match the profile.")
        total += len(data)
        if total > 64 * 1024 * 1024:
            raise ValueError("The control files exceed their aggregate limit.")
        actual.append((path, mode, len(data), hashlib.sha256(data).hexdigest()))
    for profile, (_, expected, _) in _PROFILES.items():
        if tuple(actual) == expected:
            return profile
    raise ValueError("The control files do not match an admitted profile.")


def _pattern_matches(pattern: str, path: str) -> bool:
    return path.startswith(pattern[:-2]) if pattern.endswith("/**") else path == pattern


def _policy_matches(policy: dict[str, Any], path: str) -> bool:
    return any(_pattern_matches(p, path) for p in policy["include"]) and not any(
        _pattern_matches(p, path) for p in policy.get("exclude", ())
    )


def _contract_selection(
    changes: tuple[tuple[str, str], ...], selection: dict[str, Any]
) -> set[str]:
    candidates = selection["candidates"]
    selected: set[str] = set()
    for status, path in changes:
        forbidden = path in selection["forbidden"] or any(
            _pattern_matches(pattern, path) for pattern in selection["retired"]
        )
        if forbidden and status != "D":
            raise ValueError("The changed path is forbidden by the profile.")
        matches = [p for p in selection["policies"] if _policy_matches(p, path)]
        excluded = any(_pattern_matches(p, path) for p in candidates["exclude"])
        candidate = not excluded and (
            path in candidates["files"]
            or any(path.startswith(prefix) for prefix in candidates["prefixes"])
        )
        if (
            (candidate and len(matches) != 1)
            or (excluded and matches)
            or (matches and not candidate)
            or len(matches) > 1
        ):
            raise ValueError("The changed path has no unambiguous deployment policy.")
        for policy in matches:
            if policy.get("forbidden", False) and status != "D":
                raise ValueError("The changed path is forbidden by its deployment policy.")
            selected.update(policy["validators"])
        for policy in selection["ci_policies"]:
            if _policy_matches(policy, path):
                selected.update(policy["validators"])
    return {name for name in selected if name in selection["contracts"]}


def comet_validation_checks(
    profile: str, changes: tuple[tuple[str, str], ...]
) -> tuple[RepositoryValidationCheck, ...]:
    """Select every applicable repository check without executing the selector."""
    comet_profile_digest(profile)
    _, controls, selection = _PROFILES[profile]
    if type(changes) is not tuple or len(changes) > 4096:
        raise ValueError("The change inventory is invalid.")
    seen: set[str] = set()
    for entry in changes:
        if type(entry) is not tuple or len(entry) != 2:
            raise ValueError("The change record is invalid.")
        status, path = entry
        if type(status) is not str or status not in {"A", "M", "D"}:
            raise ValueError("The change status is unsupported.")
        if not _valid_path(path) or path in seen:
            raise ValueError("The change path is invalid or repeated.")
        seen.add(path)
    commands: list[tuple[str, tuple[str, ...]]] = []
    if any(path.endswith(".py") or path in {"pyproject.toml", "uv.lock"} for path in seen):
        commands.extend(_python_commands(profile))
    if any(
        path.startswith("docs/") or path.endswith(".md") or path == "mkdocs.yml" for path in seen
    ):
        commands.append(("comet.docs.strict", (*_PREFIX, "mkdocs", "build", "--strict")))
    for validator in sorted(_contract_selection(changes, selection)):
        commands.append(
            (
                f"comet.contract.{validator}",
                (
                    *_PREFIX,
                    "python",
                    "scripts/check_deployed_inputs.py",
                    "validate",
                    "--validator",
                    validator,
                    "--container-engine",
                    "none",
                ),
            )
        )
    sources = tuple((path, size, digest) for path, _mode, size, digest in controls)
    return tuple(RepositoryValidationCheck(name, argv, sources) for name, argv in commands)


def comet_ci_check_ids(
    checks: tuple[RepositoryValidationCheck, ...], *, profile: str = COMET_PROFILE_ID
) -> tuple[str, ...]:
    """Return applicable checks that the ordinary PR workflow can cover."""
    comet_profile_digest(profile)
    selection = _PROFILES[profile][2]
    return tuple(
        check.check_id
        for check in checks
        if not check.check_id.startswith("comet.contract.")
        or selection["contracts"].get(check.check_id.removeprefix("comet.contract.")) == "pr"
    )


def comet_local_check_ids(checks: tuple[RepositoryValidationCheck, ...]) -> tuple[str, ...]:
    """Exclude the fixed validator that unconditionally requires a download."""
    return tuple(
        check.check_id for check in checks if check.check_id != "comet.contract.workflow-contracts"
    )


class _CometGitSource:
    """Read immutable Git objects within one deadline and fixed output limits."""

    def __init__(self, root: Path, timeout_s: float, shutdown: threading.Event | None) -> None:
        self.root = root
        self.deadline = time.monotonic() + timeout_s
        self.shutdown = shutdown
        self.env = _controlled_git_env()
        self.env.update(
            {
                "GIT_NO_REPLACE_OBJECTS": "1",
                "GIT_GRAFT_FILE": os.devnull,
                "GIT_NO_LAZY_FETCH": "1",
            }
        )

    def read(self, *args: str, limit: int) -> str:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("The Comet source inspection deadline expired.")
        result = _run_bounded_git_output(
            ("git", "--no-replace-objects", *args),
            cwd=self.root,
            timeout=remaining,
            max_bytes=limit,
            retain_text=True,
            env=self.env,
            shutdown=self.shutdown,
        )
        # The shared reader preserves arbitrary bytes. This profile requires UTF-8.
        result.text.encode("utf-8", errors="strict")
        return result.text

    def admit_revision(self, revision: str) -> str:
        tree = self.read("ls-tree", "-r", "-z", "--full-tree", revision, limit=64 * 1024 * 1024)
        if not tree or not tree.endswith("\0"):
            raise ValueError("The Git source inventory is incomplete.")
        inventory: list[str] = []
        members: dict[str, tuple[int, str]] = {}
        for record in tree[:-1].split("\0"):
            metadata, separator, path = record.partition("\t")
            fields = metadata.split(" ")
            if (
                not separator
                or len(fields) != 3
                or re.fullmatch(r"[0-7]{6}", fields[0]) is None
                or fields[1] not in {"blob", "commit"}
                or re.fullmatch(r"[0-9a-f]{40}", fields[2]) is None
                or not _valid_path(path)
                or path in members
            ):
                raise ValueError("The Git source inventory record is invalid.")
            mode = int(fields[0], 8)
            if mode not in {0o100644, 0o100755} or fields[1] != "blob":
                raise ValueError("The Git source inventory contains a nonregular file.")
            inventory.append(path)
            members[path] = (mode, fields[2])
            if len(inventory) > 500_000:
                raise ValueError("The Git source inventory exceeds its limit.")
        if not members.keys() >= _BASE_CONTROL_PATHS:
            raise ValueError("The Git control inventory is incomplete.")
        controls: dict[str, tuple[int, bytes]] = {}
        for path in sorted(members.keys() & _CONTROL_PATHS):
            mode, object_id = members[path]
            object_size = self.read("cat-file", "-s", object_id, limit=32).strip()
            if object_size not in {str(value) for value in _CONTROL_SIZES[path]}:
                raise ValueError("The Git control size does not match the profile.")
            data = self.read("cat-file", "blob", object_id, limit=int(object_size) + 1).encode(
                "utf-8"
            )
            controls[path] = (mode, data)
        return admit_comet_controls(controls, tuple(inventory))

    def changes(self, base: str, head: str) -> tuple[str, tuple[tuple[str, str], ...]]:
        branchpoint = self.read("merge-base", "--all", base, head, limit=128).strip()
        if re.fullmatch(r"[0-9a-f]{40}", branchpoint) is None:
            raise ValueError("The review has no unique immutable merge base.")
        raw = self.read(
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-status",
            "-z",
            branchpoint,
            head,
            "--",
            limit=8 * 1024 * 1024,
        )
        if not raw:
            return branchpoint, ()
        if not raw.endswith("\0"):
            raise ValueError("The Git change inventory is incomplete.")
        fields = raw[:-1].split("\0")
        if len(fields) % 2 or len(fields) > 8192:
            raise ValueError("The Git change inventory exceeds its record limit.")
        return branchpoint, tuple(zip(fields[::2], fields[1::2], strict=True))


def comet_plan_for_workspace(
    workspace: WorkspaceBinding,
    *,
    issue_number: int | None,
    pr_number: int,
    reviewed_base: str,
    timeout_s: float,
    shutdown: threading.Event | None = None,
) -> RepositoryValidationPlan:
    """Bind admitted controls and real change statuses to immutable Git commits.

    The caller holds the source lease. The PR target commit remains distinct
    from the merge base used to collect changes. No worktree file is executed.
    """
    from dataclasses import replace

    if type(workspace) is not WorkspaceBinding or not isinstance(workspace.revision, str):
        raise ValueError("The source binding type is invalid.")
    plan = RepositoryValidationPlan(
        repository="LLM360/comet",
        issue_number=issue_number,
        pr_number=pr_number,
        reviewed_head=workspace.revision,
        reviewed_base=reviewed_base,
        source_workspace=workspace,
        changes=(),
        profile_id=COMET_PROFILE_ID,
        profile_digest=COMET_PROFILE_DIGEST,
        checks=(),
        execution_allowed=False,
        coverage_reason="Immutable source inspection is incomplete.",
    )
    if type(timeout_s) not in {int, float} or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise ValueError("The source inspection timeout is invalid.")
    source = _CometGitSource(workspace.cwd, min(timeout_s, 120), shutdown)
    if source.read("rev-parse", "--is-shallow-repository", limit=16).strip() != "false":
        raise ValueError("A complete Git history is necessary for source inspection.")
    profiles = []
    for revision in (plan.reviewed_head, plan.reviewed_base):
        commit = source.read("rev-parse", "--verify", f"{revision}^{{commit}}", limit=64).strip()
        if commit != revision:
            raise ValueError("The source revision is not an immutable commit.")
        profiles.append(source.admit_revision(revision))
    if profiles[0] != profiles[1]:
        raise ValueError("The source revisions have different control profiles.")
    profile = profiles[0]
    diff_base, changes = source.changes(plan.reviewed_base, plan.reviewed_head)
    checks = comet_validation_checks(profile, changes)
    return replace(
        plan,
        profile_id=profile,
        profile_digest=comet_profile_digest(profile),
        changes=changes,
        checks=checks,
        diff_base_sha=diff_base,
        execution_allowed=bool(checks),
        coverage_reason="" if checks else "No applicable validation check was selected.",
    )


_CI_ID = r"[1-9][0-9]{0,19}"
_CI_SHA = r"[0-9a-f]{40}"
_CI_ENDPOINT = re.compile(
    r"repos/LLM360/comet/(?:"
    rf"pulls/{_CI_ID}"
    rf"|actions/runs/{_CI_ID}"
    rf"|actions/runs/{_CI_ID}/attempts/{_CI_ID}/jobs\?per_page=100&page=(?:[1-9]|10)"
    rf"|actions/workflows/ci\.yml/runs\?event=pull_request&head_sha={_CI_SHA}&per_page=100&page=[1-5]"
    rf"|git/commits/{_CI_SHA}"
    rf"|git/trees/{_CI_SHA}(?:\?recursive=1)?"
    rf"|git/blobs/{_CI_SHA}"
    r")"
)
_CI_RESPONSE_BYTES = 4 * 1024 * 1024
_CI_AGGREGATE_BYTES = 32 * 1024 * 1024


class CometCIReadError(RuntimeError):
    """Report a bounded CI failure code without provider output."""


def _ci_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("The provider JSON repeats a member.")
        result[key] = value
    return result


def _ci_json_constant(value: str) -> NoReturn:
    raise ValueError("The provider JSON contains a nonfinite constant.")


def _ci_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("The provider JSON contains a nonfinite number.")
    return number


def _ci_json_text_is_valid(value: object) -> None:
    pending = [value]
    while pending:
        member = pending.pop()
        if isinstance(member, str):
            member.encode("utf-8", errors="strict")
        elif isinstance(member, dict):
            pending.extend(member.keys())
            pending.extend(member.values())
        elif isinstance(member, list):
            pending.extend(member)


class CometCIReader:
    """Keep all provider reads inside one deadline and aggregate budget."""

    def __init__(self, *, deadline_s: float, shutdown: threading.Event | None = None) -> None:
        """Capture the fixed request deadline without starting a child process."""
        if type(deadline_s) not in {int, float} or not math.isfinite(deadline_s) or deadline_s <= 0:
            raise ValueError("The CI request deadline is invalid.")
        self.deadline_s = min(deadline_s, time.monotonic() + 120)
        self.shutdown = shutdown
        self._reads = 0
        self._objects = 0
        self._json_bytes = 0
        self._failure: str | None = None
        self._executable = trusted_gh_executable()

    def _fail(self, code: str) -> NoReturn:
        self._failure = code
        raise CometCIReadError(code) from None

    def _remaining(self) -> float:
        if self._failure is not None:
            self._fail(self._failure)
        if self.shutdown is not None and self.shutdown.is_set():
            self._fail("ci_request_cancelled")
        remaining = self.deadline_s - time.monotonic()
        if remaining <= 0:
            self._fail("ci_request_deadline")
        return remaining

    def _run(self, endpoint: str, remaining: float, output_limit: int) -> str:
        if self._executable is None:
            self._fail("ci_cli_unavailable")
        try:
            result = run_subprocess(
                [
                    str(self._executable),
                    "api",
                    "--hostname",
                    "github.com",
                    "--method",
                    "GET",
                    endpoint,
                ],
                env=build_gh_child_env(),
                timeout=remaining,
                check=False,
                log_on_error=False,
                track_process_group=True,
                shutdown=self.shutdown,
                remaining_timeout=self._remaining,
                max_output_bytes=output_limit,
            )
        except SubprocessOutputLimitExceeded:
            self._fail(
                "ci_aggregate_byte_limit"
                if output_limit < _CI_RESPONSE_BYTES
                else "ci_response_byte_limit"
            )
        except subprocess.TimeoutExpired:
            self._fail("ci_request_deadline")
        except InterruptedError:
            self._fail("ci_request_cancelled")
        except CometCIReadError:
            raise
        except (OSError, RuntimeError, subprocess.SubprocessError):
            self._fail("ci_transport_failure")
        self._remaining()
        if result.returncode != 0:
            self._fail(
                "ci_rate_limit" if "rate limit" in result.stderr.lower() else "ci_provider_failure"
            )
        return result.stdout

    def read(self, endpoint: str) -> dict[str, Any]:
        """Read one finite-profile endpoint with no pagination or retry expansion."""
        remaining = self._remaining()
        if type(endpoint) is not str or _CI_ENDPOINT.fullmatch(endpoint) is None:
            self._fail("ci_endpoint_invalid")
        if self._reads >= 600:
            self._fail("ci_request_count_limit")
        is_object = "/git/trees/" in endpoint or "/git/blobs/" in endpoint
        if is_object and self._objects >= 512:
            self._fail("ci_object_count_limit")
        available = _CI_AGGREGATE_BYTES - self._json_bytes
        if available <= 0:
            self._fail("ci_aggregate_byte_limit")
        output_limit = min(available, _CI_RESPONSE_BYTES)
        self._reads += 1
        self._objects += int(is_object)
        text = self._run(endpoint, remaining, output_limit)
        try:
            encoded_size = len(text.encode("utf-8", errors="strict"))
            if encoded_size > output_limit:
                self._fail("ci_response_byte_limit")
            self._json_bytes += encoded_size
            # The shared process reader replaces invalid UTF-8. Do not admit
            # that replacement as provider evidence.
            if "\ufffd" in text:
                raise ValueError("The provider JSON contains replacement text.")
            value = json.loads(
                text,
                object_pairs_hook=_ci_json_object,
                parse_constant=_ci_json_constant,
                parse_float=_ci_json_float,
            )
            if not isinstance(value, dict):
                raise ValueError("The provider JSON root is not an object.")
            _ci_json_text_is_valid(value)
        except (ValueError, RecursionError):
            self._fail("ci_json_invalid")
        self._remaining()
        return value


_CI_LOCAL_WORKFLOWS = frozenset(
    f".github/workflows/{name}.yml"
    for name in ("contracts", "secrets", "docs-check", "postgres-tests", "build-image")
)
_CI_STATUSES = frozenset({"queued", "in_progress", "completed", "waiting", "requested", "pending"})
_CI_CONCLUSIONS = frozenset(
    {
        "success",
        "failure",
        "neutral",
        "cancelled",
        "skipped",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }
)


@dataclass(frozen=True, slots=True)
class CometCIIdentity:
    """Bind provider identity facts without claiming command or source coverage."""

    repository_id: int
    pr_number: int
    reviewed_head: str
    reviewed_base: str
    head_branch: str
    run_id: int
    run_attempt: int
    status: str
    conclusion: str | None
    merge_sha: str
    merge_tree: str
    workflows: tuple[tuple[str, str, str], ...]


def _ci_require(condition: bool) -> None:
    if not condition:
        raise CometCIReadError("ci_identity_invalid")


def _ci_object(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise CometCIReadError("ci_identity_invalid")
    return value


def _ci_positive(value: object) -> bool:
    return type(value) is int and value > 0


def _ci_full_sha(value: object) -> bool:
    return type(value) is str and re.fullmatch(_CI_SHA, value) is not None


def _ci_repository_id(value: object) -> int:
    repo = _ci_object(value)
    name = repo.get("full_name")
    identifier = repo.get("id")
    _ci_require(
        type(name) is str
        and name.casefold() == "llm360/comet"
        and _ci_positive(identifier)
        and repo.get("fork") is False
    )
    return cast(int, identifier)


def comet_ci_identity(
    pr: object,
    run: object,
    merge_commit: object,
    *,
    pr_number: int,
    reviewed_head: str,
    reviewed_base: str,
    head_branch: str,
) -> CometCIIdentity:
    """Check the retained PR, run, and immutable merge witness as one identity.

    This result does not prove control bytes, job steps, or stable reads. The
    provider adapter must prove those facts before it issues any receipt.
    """
    _ci_require(
        _ci_positive(pr_number)
        and _ci_full_sha(reviewed_head)
        and _ci_full_sha(reviewed_base)
        and type(head_branch) is str
        and 0 < len(head_branch) <= 1024
        and not any(ord(char) < 32 or ord(char) == 127 for char in head_branch)
    )
    pull = _ci_object(pr)
    workflow = _ci_object(run)
    merge = _ci_object(merge_commit)
    head = _ci_object(pull.get("head"))
    base = _ci_object(pull.get("base"))
    _ci_require(
        _ci_positive(pull.get("number"))
        and pull["number"] == pr_number
        and pull.get("state") == "open"
        and head.get("sha") == reviewed_head
        and head.get("ref") == head_branch
        and base.get("sha") == reviewed_base
        and base.get("ref") == "main"
    )
    repository_id = _ci_repository_id(base.get("repo"))
    _ci_require(
        all(
            _ci_repository_id(repo) == repository_id
            for repo in (
                head.get("repo"),
                workflow.get("repository"),
                workflow.get("head_repository"),
            )
        )
    )
    run_id = workflow.get("id")
    attempt = workflow.get("run_attempt")
    status = workflow.get("status")
    conclusion = workflow.get("conclusion")
    _ci_require(
        _ci_positive(run_id)
        and _ci_positive(attempt)
        and workflow.get("head_sha") == reviewed_head
        and workflow.get("head_branch") == head_branch
        and workflow.get("path") == ".github/workflows/ci.yml"
        and workflow.get("event") == "pull_request"
        and type(status) is str
        and status in _CI_STATUSES
        and (conclusion is None or (type(conclusion) is str and conclusion in _CI_CONCLUSIONS))
        and ((status == "completed") == (conclusion is not None))
    )
    witnesses = workflow.get("referenced_workflows")
    _ci_require(type(witnesses) is list and len(witnesses) == len(_CI_LOCAL_WORKFLOWS))
    records: list[tuple[str, str, str]] = []
    paths: set[str] = set()
    merge_shas: set[str] = set()
    expected_ref = f"refs/pull/{pr_number}/merge"
    for item in cast(list[object], witnesses):
        witness = _ci_object(item)
        path, sha, ref = (witness.get(key) for key in ("path", "sha", "ref"))
        _ci_require(type(path) is str and _ci_full_sha(sha) and ref == expected_ref)
        source_path, separator, source_sha = cast(str, path).partition("@")
        _ci_require(
            separator == "@" and source_sha == sha and source_path.startswith("LLM360/comet/")
        )
        local_path = source_path.removeprefix("LLM360/comet/")
        _ci_require(local_path in _CI_LOCAL_WORKFLOWS and local_path not in paths)
        paths.add(local_path)
        merge_shas.add(cast(str, sha))
        records.append((local_path, cast(str, sha), expected_ref))
    _ci_require(len(merge_shas) == 1)
    merge_sha = next(iter(merge_shas))
    parents = merge.get("parents")
    _ci_require(merge.get("sha") == merge_sha and type(parents) is list and len(parents) == 2)
    _ci_require(
        [_ci_object(parent).get("sha") for parent in cast(list[object], parents)]
        == [reviewed_base, reviewed_head]
    )
    tree_sha = _ci_object(merge.get("tree")).get("sha")
    _ci_require(_ci_full_sha(tree_sha))
    return CometCIIdentity(
        repository_id=repository_id,
        pr_number=pr_number,
        reviewed_head=reviewed_head,
        reviewed_base=reviewed_base,
        head_branch=head_branch,
        run_id=cast(int, run_id),
        run_attempt=cast(int, attempt),
        status=cast(str, status),
        conclusion=conclusion,
        merge_sha=merge_sha,
        merge_tree=cast(str, tree_sha),
        workflows=tuple(sorted(records)),
    )


class CometCIControls:
    """Check immutable control objects with a cache limited to one request."""

    def __init__(self, reader: CometCIReader) -> None:
        """Use the existing reader and its complete-request resource limits."""
        self._reader = reader
        self._commits: dict[str, str] = {}
        self._profiles: dict[str, str] = {}
        self._blobs: dict[str, bytes] = {}
        self._decoded_bytes = 0

    def admit(self, commit_sha: str, *, expected_tree: str | None = None) -> str:
        """Require complete controls at the requested immutable commit."""
        self._reader._remaining()
        try:
            if not _ci_full_sha(commit_sha) or (
                expected_tree is not None and not _ci_full_sha(expected_tree)
            ):
                raise ValueError("The source object identity is invalid.")
            tree_sha = self._commits.get(commit_sha)
            if tree_sha is None:
                commit = self._reader.read(f"repos/LLM360/comet/git/commits/{commit_sha}")
                tree = commit.get("tree")
                if (
                    commit.get("sha") != commit_sha
                    or type(tree) is not dict
                    or not _ci_full_sha(tree.get("sha"))
                ):
                    raise ValueError("The source commit is invalid.")
                tree_sha = cast(str, tree["sha"])
                self._commits[commit_sha] = tree_sha
            if expected_tree is not None and tree_sha != expected_tree:
                raise ValueError("The source tree does not match the witness.")
            if tree_sha not in self._profiles:
                self._profiles[tree_sha] = self._admit_tree(tree_sha)
            profile = self._profiles[tree_sha]
        except (ValueError, TypeError, KeyError):
            raise CometCIReadError("ci_source_invalid") from None
        self._reader._remaining()
        return profile

    def _admit_tree(self, tree_sha: str) -> str:
        tree = self._reader.read(f"repos/LLM360/comet/git/trees/{tree_sha}?recursive=1")
        entries = tree.get("tree")
        if (
            tree.get("sha") != tree_sha
            or tree.get("truncated") is not False
            or type(entries) is not list
            or len(entries) > 500_000
        ):
            raise ValueError("The source tree is incomplete.")
        directories: set[str] = set()
        members: dict[str, tuple[int, str, int]] = {}
        seen: set[str] = set()
        path_bytes = 0
        for entry in entries:
            if type(entry) is not dict:
                raise ValueError("The source tree member is invalid.")
            path = entry.get("path")
            if not _valid_path(path) or path in seen or not _ci_full_sha(entry.get("sha")):
                raise ValueError("The source path is invalid.")
            path = cast(str, path)
            seen.add(path)
            path_bytes += len(path.encode("utf-8")) + 1
            if path_bytes > 64 * 1024 * 1024:
                raise ValueError("The source path inventory exceeds its limit.")
            if entry.get("mode") == "040000" and entry.get("type") == "tree":
                directories.add(path)
                continue
            size = entry.get("size")
            if (
                entry.get("mode") not in ("100644", "100755")
                or entry.get("type") != "blob"
                or type(size) is not int
                or size < 0
            ):
                raise ValueError("The source file is not regular.")
            members[path] = (int(entry["mode"], 8), entry["sha"], size)
        if any(
            str(parent) != "." and str(parent) not in directories
            for path in seen
            for parent in PurePosixPath(path).parents
        ):
            raise ValueError("The source tree has a missing parent directory.")
        if not members.keys() >= _BASE_CONTROL_PATHS or len(_CONTROL_PATHS) > 256:
            raise ValueError("The source control inventory is incomplete.")
        controls: dict[str, tuple[int, bytes]] = {}
        for path in sorted(members.keys() & _CONTROL_PATHS):
            mode, blob_sha, size = members[path]
            if (
                mode not in _CONTROL_MODES[path]
                or size not in _CONTROL_SIZES[path]
                or size > 16 * 1024 * 1024
            ):
                raise ValueError("The source control metadata changed.")
            controls[path] = (mode, self._blob(blob_sha, size))
        return admit_comet_controls(controls, tuple(members))

    def _blob(self, blob_sha: str, expected_size: int) -> bytes:
        cached = self._blobs.get(blob_sha)
        if cached is not None:
            if len(cached) != expected_size:
                raise ValueError("The cached source size is inconsistent.")
            return cached
        if len(self._blobs) >= 256 or self._decoded_bytes + expected_size > 64 * 1024 * 1024:
            raise ValueError("The source control cache exceeds its limit.")
        blob = self._reader.read(f"repos/LLM360/comet/git/blobs/{blob_sha}")
        content = blob.get("content")
        if (
            blob.get("sha") != blob_sha
            or type(blob.get("size")) is not int
            or blob["size"] != expected_size
            or blob.get("encoding") != "base64"
            or type(content) is not str
        ):
            raise ValueError("The source blob metadata is invalid.")
        data = base64.b64decode(content.replace("\n", ""), validate=True)
        if (
            len(data) != expected_size
            or hashlib.sha1(
                f"blob {len(data)}\0".encode("ascii") + data, usedforsecurity=False
            ).hexdigest()
            != blob_sha
        ):
            raise ValueError("The source blob digest is invalid.")
        self._blobs[blob_sha] = data
        self._decoded_bytes += len(data)
        return data


@dataclass(frozen=True, slots=True)
class CometCICollection:
    """Return only receipts admitted by the complete provider proof."""

    receipts: tuple[RepositoryValidationReceipt, ...] = ()
    gaps: tuple[RepositoryValidationGap, ...] = ()


@dataclass(frozen=True, slots=True)
class _CometCIStep:
    number: int
    name: str
    status: str
    conclusion: str | None


@dataclass(frozen=True, slots=True)
class _CometCIJob:
    identifier: int
    name: str
    status: str
    conclusion: str | None
    steps: tuple[_CometCIStep, ...]


def _ci_pages(
    reader: CometCIReader, endpoint: str, member: str, page_limit: int
) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    seen: set[int] = set()
    total: int | None = None
    for page in range(1, page_limit + 1):
        response = reader.read(f"{endpoint}&page={page}")
        count, items = response.get("total_count"), response.get(member)
        if (
            type(count) is not int
            or not 0 <= count <= page_limit * 100
            or (total is not None and total != count)
            or type(items) is not list
            or len(items) != min(100, count - len(records))
            or response.get("truncated", False) is not False
        ):
            raise CometCIReadError("ci_pagination_invalid")
        total = count
        for item in items:
            if type(item) is not dict or not _ci_positive(item.get("id")) or item["id"] in seen:
                raise CometCIReadError("ci_pagination_invalid")
            seen.add(item["id"])
            records.append(item)
        if len(records) == total:
            reader._remaining()
            return tuple(records)
    raise CometCIReadError("ci_pagination_limit")


def _ci_status(record: dict[str, Any]) -> tuple[str, str | None]:
    status, conclusion = record.get("status"), record.get("conclusion")
    if (
        type(status) is not str
        or status not in _CI_STATUSES
        or (
            conclusion is not None
            and (type(conclusion) is not str or conclusion not in _CI_CONCLUSIONS)
        )
        or ((status == "completed") != (conclusion is not None))
    ):
        raise CometCIReadError("ci_job_status_invalid")
    return status, conclusion


def _ci_step(value: object) -> _CometCIStep:
    record = _ci_object(value)
    number, name = record.get("number"), record.get("name")
    if not _ci_positive(number) or type(name) is not str or not 0 < len(name) <= 2048:
        raise CometCIReadError("ci_step_invalid")
    status, conclusion = _ci_status(record)
    return _CometCIStep(cast(int, number), name, status, conclusion)


def _ci_jobs(
    records: tuple[dict[str, Any], ...], identity: CometCIIdentity
) -> tuple[_CometCIJob, ...]:
    jobs: list[_CometCIJob] = []
    for record in records:
        name, steps = record.get("name"), record.get("steps")
        if (
            not _ci_positive(record.get("id"))
            or type(record.get("run_id")) is not int
            or record["run_id"] != identity.run_id
            or type(record.get("run_attempt")) is not int
            or record["run_attempt"] != identity.run_attempt
            or record.get("head_sha") != identity.reviewed_head
            or type(name) is not str
            or not 0 < len(name) <= 1024
            or type(steps) is not list
            or len(steps) > 100
        ):
            raise CometCIReadError("ci_job_identity_invalid")
        normalized = tuple(sorted((_ci_step(step) for step in steps), key=lambda step: step.number))
        if len({step.number for step in normalized}) != len(normalized):
            raise CometCIReadError("ci_step_duplicate")
        status, conclusion = _ci_status(record)
        jobs.append(_CometCIJob(record["id"], name, status, conclusion, normalized))
    return tuple(sorted(jobs, key=lambda job: job.identifier))


def _ci_observation(
    reader: CometCIReader,
    controls: CometCIControls,
    invocation: RepositoryValidationInvocation,
    head_branch: str,
    run_id: int,
    attempt: int,
) -> tuple[CometCIIdentity, tuple[_CometCIJob, ...]]:
    plan = invocation.plan
    pull = reader.read(f"repos/LLM360/comet/pulls/{plan.pr_number}")
    run_endpoint = f"repos/LLM360/comet/actions/runs/{run_id}"
    run = reader.read(run_endpoint)
    if run.get("id") != run_id or run.get("run_attempt") != attempt:
        raise CometCIReadError("ci_run_changed")
    witnesses = run.get("referenced_workflows")
    if type(witnesses) is not list or not witnesses or type(witnesses[0]) is not dict:
        raise CometCIReadError("ci_merge_witness_invalid")
    merge_sha = witnesses[0].get("sha")
    if not _ci_full_sha(merge_sha):
        raise CometCIReadError("ci_merge_witness_invalid")
    merge = reader.read(f"repos/LLM360/comet/git/commits/{merge_sha}")
    identity = comet_ci_identity(
        pull,
        run,
        merge,
        pr_number=plan.pr_number,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        head_branch=head_branch,
    )
    for sha, tree in (
        (plan.reviewed_head, None),
        (plan.reviewed_base, None),
        (identity.merge_sha, identity.merge_tree),
    ):
        if controls.admit(sha, expected_tree=tree) != plan.profile_id:
            raise CometCIReadError("ci_profile_changed")
    records = _ci_pages(reader, f"{run_endpoint}/attempts/{attempt}/jobs?per_page=100", "jobs", 10)
    jobs = _ci_jobs(records, identity)
    after = comet_ci_identity(
        pull,
        reader.read(run_endpoint),
        merge,
        pr_number=plan.pr_number,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        head_branch=head_branch,
    )
    if after != identity:
        raise CometCIReadError("ci_run_changed")
    reader._remaining()
    return identity, jobs


_CI_CHECKOUT_STEP = "Run actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_CI_UV_STEP = "Run astral-sh/setup-uv@bec219d24cd3e171d82865faccec33120bb574f4"
_CI_FAILED_CONCLUSIONS = _CI_CONCLUSIONS - {"success", "neutral", "skipped"}


def _ci_check_steps(
    check: RepositoryValidationCheck, profile: str = COMET_PROFILE_ID
) -> tuple[str, tuple[tuple[str, str], ...]]:
    prefix = [("Set up job", "success"), (_CI_CHECKOUT_STEP, "success"), (_CI_UV_STEP, "success")]
    match check.check_id:
        case "comet.python.ruff-format":
            name, target = "lint", "Run " + " ".join(check.argv)
        case "comet.python.ruff-check":
            prefix.append(("Run " + " ".join(_python_commands(profile)[0][1]), "success"))
            name, target = "lint", "Run " + " ".join(check.argv)
        case "comet.python.ty-check":
            name, target = "typecheck", "Run " + " ".join(check.argv)
        case "comet.python.pr-tests":
            prefix.append(("Install Bubblewrap", "success"))
            name, target = "test (3.12)", "Run the ordinary pull-request profile"
        case "comet.docs.strict":
            name, target = "docs / docs-strict", "Build site in strict mode"
        case _:
            validator = check.check_id.removeprefix("comet.contract.")
            if (
                check.check_id == validator
                or _PROFILES[profile][2]["contracts"].get(validator) != "pr"
            ):
                raise CometCIReadError("ci_check_unsupported")
            prefix.insert(2, ("Verify the checked-out commit", "success"))
            prefix.extend(
                (
                    (
                        "Install Bubblewrap",
                        "success" if validator == "workflow-contracts" else "skipped",
                    ),
                    ("Successful no-op", "skipped"),
                )
            )
            name, target = f"contracts / {validator}", "Validate selected contract"
    return name, (*prefix, (target, "success"))


def _ci_job_status(
    jobs: tuple[_CometCIJob, ...], name: str, expected: tuple[tuple[str, str], ...]
) -> Literal["success", "failed"] | None:
    matching = [job for job in jobs if job.name == name]
    if len(matching) > 1:
        raise CometCIReadError("ci_job_ambiguous")
    if not matching:
        return None
    job = matching[0]
    names = {name for name, _ in expected}
    if job.conclusion in _CI_FAILED_CONCLUSIONS or any(
        step.name in names and step.conclusion in _CI_FAILED_CONCLUSIONS for step in job.steps
    ):
        return "failed"
    if (
        job.status == "completed"
        and job.conclusion == "success"
        and tuple((step.name, step.conclusion) for step in job.steps[: len(expected)]) == expected
        and all(step.status == "completed" for step in job.steps[: len(expected)])
        and all(sum(step.name == name for step in job.steps) == 1 for name in names)
    ):
        return "success"
    return None


def _ci_legacy_scope_proved(plan: RepositoryValidationPlan, jobs: tuple[_CometCIJob, ...]) -> bool:
    # The admitted main-target PR workflow excludes promotion. Its pinned
    # classifier selects legacy before content rules for these complete paths.
    if any(
        path == ".github/ci/k2-affected-scope.json"
        or any(
            re.search(r"(?:^|[-_.])k2(?:$|[-_.])", part, re.IGNORECASE) for part in path.split("/")
        )
        for _status, path in plan.changes
    ):
        return False
    expected = (
        ("Set up job", "success"),
        (_CI_CHECKOUT_STEP, "success"),
        (_CI_UV_STEP, "success"),
        ("Initialize safe downstream selections", "success"),
        ("Collect immutable changed-path records", "success"),
        ("Validate inventory and classify inputs", "success"),
    )
    return _ci_job_status(jobs, "deployment-policy", expected) == "success"


def _ci_complete_pr_shards(jobs: tuple[_CometCIJob, ...]) -> Literal["success", "failed"] | None:
    names = {f"test (3.12, shard {index})" for index in range(32)}
    if any(job.name.startswith("test (3.12, shard ") and job.name not in names for job in jobs):
        raise CometCIReadError("ci_job_ambiguous")
    expected = (
        ("Set up job", "success"),
        (_CI_CHECKOUT_STEP, "success"),
        (_CI_UV_STEP, "success"),
        ("Require the test scope", "skipped"),
        ("Verify test source", "success"),
        ("Install Bubblewrap", "success"),
        ("Run the ordinary pull-request profile", "success"),
        ("Run the promotion profile", "skipped"),
    )
    statuses = [_ci_job_status(jobs, name, expected) for name in sorted(names)]
    if "failed" in statuses:
        return "failed"
    return "success" if all(status == "success" for status in statuses) else None


def _ci_check_receipt(
    plan: RepositoryValidationPlan, check: RepositoryValidationCheck, jobs: tuple[_CometCIJob, ...]
) -> RepositoryValidationReceipt | None:
    current = plan.profile_id == comet_profile_7c2772e.PROFILE_ID
    full_tests = check.check_id == "comet.python.pr-tests" or check.check_id.startswith(
        "comet.contract."
    )
    if current and full_tests and not _ci_legacy_scope_proved(plan, jobs):
        return None
    if current and check.check_id == "comet.python.pr-tests":
        status = _ci_complete_pr_shards(jobs)
    else:
        name, expected = _ci_check_steps(check, plan.profile_id)
        status = _ci_job_status(jobs, name, expected)
    if status is None:
        return None
    return RepositoryValidationReceipt(
        repository=plan.repository,
        pr_number=plan.pr_number,
        plan_id=plan.plan_id,
        check_id=check.check_id,
        reviewed_head=plan.reviewed_head,
        reviewed_base=plan.reviewed_base,
        argv=check.argv,
        source_digests=check.source_digests,
        evidence_kind="ci",
        status=status,
    )


def collect_comet_ci(
    invocation: RepositoryValidationInvocation, *, head_branch: str, reader: CometCIReader
) -> CometCICollection:
    """Require two complete provider observations before issuing CI receipts."""
    try:
        if (
            type(invocation) is not RepositoryValidationInvocation
            or replace(invocation) != invocation
        ):
            raise ValueError("The CI invocation changed.")
        plan = invocation.plan
        if (
            invocation.evidence_kind != "ci"
            or not plan.execution_allowed
            or plan.profile_digest != comet_profile_digest(plan.profile_id)
            or plan.checks != comet_validation_checks(plan.profile_id, plan.changes)
            or not set(invocation.check_ids)
            <= set(comet_ci_check_ids(plan.checks, profile=plan.profile_id))
        ):
            raise ValueError("The CI selection does not match the admitted profile.")
    except (ValueError, TypeError, AttributeError):
        return CometCICollection(gaps=(RepositoryValidationGap("*", "ci_plan_invalid"),))
    try:
        reader._remaining()
        runs = _ci_pages(
            reader,
            f"repos/LLM360/comet/actions/workflows/ci.yml/runs?event=pull_request&head_sha={plan.reviewed_head}&per_page=100",
            "workflow_runs",
            5,
        )
        if not runs:
            return CometCICollection()
        latest = max(runs, key=lambda run: run["id"])
        run_id, attempt = latest["id"], latest.get("run_attempt")
        if not _ci_positive(attempt):
            raise CometCIReadError("ci_run_attempt_invalid")
        controls = CometCIControls(reader)
        first = _ci_observation(
            reader, controls, invocation, head_branch, run_id, cast(int, attempt)
        )
        second = _ci_observation(
            reader, controls, invocation, head_branch, run_id, cast(int, attempt)
        )
        if first != second:
            raise CometCIReadError("ci_observation_changed")
        receipts = tuple(
            receipt
            for check in plan.checks
            if check.check_id in invocation.check_ids
            if (receipt := _ci_check_receipt(plan, check, first[1])) is not None
        )
        reader._remaining()
        return CometCICollection(receipts=receipts)
    except CometCIReadError as error:
        return CometCICollection(gaps=(RepositoryValidationGap("*", str(error)),))
