"""Test-only builders for registered deployment contexts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping

    from _pytest.monkeypatch import MonkeyPatch

from comet.config import (
    Cookbook,
    DonorRecord,
    GatewayInfo,
    ReplicaRecord,
    SchedulerIdentityProfileV1,
    load_cluster_profile,
    load_cookbook,
)
from comet.context_transition import context_transition_guard
from comet.deployment_context import (
    DeploymentContext,
    DeploymentContextRegistry,
    DeploymentContextSpec,
    _RegisteredContext,
)
from comet.paths import (
    CometPaths,
    PublicDeploymentPaths,
    RuntimeEnvironment,
    atomic_write_json,
)
from comet.public_context import PublicContextV1, PublicRuntime
from comet.root_locator import ResolvedRootIdentity
from comet.runtime_artifact import pool_runtime_provenance
from comet.scheduler_identity import SchedulerContextEvidenceV2, SlurmObservationV2
from comet.stack_release import StackRelease
from comet.stack_runtime import _activate_stack_release_locked, cluster_stack_stamp

REPO = Path(__file__).parent.parent
CONTEXTS = REPO / "deployment" / "contexts"
TEST_ENGINE_API_KEY = "ck_" + ("T" * 40)
TEST_INGEST_API_KEY = "ck_" + ("I" * 40)


def set_ingest_credential_environment(
    monkeypatch: MonkeyPatch,
    context: DeploymentContext,
    *,
    host: str = "ingest.test",
    token: str = TEST_INGEST_API_KEY,
) -> str:
    """Install one complete typed test ingest credential in the environment."""
    url = f"http://{host}:{context.spec.endpoint.ports.ingest}"
    monkeypatch.setenv("COMET_INGEST_URL", url)
    monkeypatch.setenv("COMET_INGEST_TOKEN", token)
    monkeypatch.setenv("COMET_INGEST_TOKEN_CLASS", "local-deployment")
    monkeypatch.setenv("COMET_INGEST_TOKEN_CLUSTER", context.cluster_id)
    monkeypatch.setenv("COMET_INGEST_TOKEN_DEPLOYMENT", context.deployment_id)
    monkeypatch.setenv("COMET_INGEST_TOKEN_ROLE", context.spec.credentials.machine_role)
    return url


class TestGatewayInfo(GatewayInfo):
    """Provide complete static context evidence for isolated model tests."""

    __test__: ClassVar[bool] = False
    context_id: str = "ctx-" + "1" * 64
    deployment_id: str = "test-deployment"
    release_id: str = "2" * 64
    source_sha: str = "3" * 40
    scheduler_namespace: str = "comet-test-deployment"
    endpoint_id: str = "test-endpoint"


class TestReplicaRecord(ReplicaRecord):
    """Provide complete static context evidence for isolated model tests."""

    __test__: ClassVar[bool] = False
    context_id: str = "ctx-" + "1" * 64
    deployment_id: str = "test-deployment"
    context_release_id: str = "2" * 64
    context_source_sha: str = "3" * 40
    scheduler_namespace: str = "comet-test-deployment"
    gateway_registration_protocol: Literal["transactional-v1"] = "transactional-v1"
    capacity_slots_per_replica: int = 1
    instance_id: str = "4" * 32
    stack_component_id: str = "5" * 64


class TestDonorRecord(DonorRecord):
    """Provide complete static context evidence for isolated model tests."""

    __test__: ClassVar[bool] = False
    context_id: str = "ctx-" + "1" * 64
    context_deployment_id: str = "test-deployment"
    context_release_id: str = "2" * 64
    context_source_sha: str = "3" * 40
    scheduler_namespace: str = "comet-test-deployment"
    instance_id: str = "4" * 32
    stack_component_id: str = "5" * 64


def make_test_context(
    root: Path,
    *,
    models: tuple[str, ...] = ("qwen3-8b",),
    cluster_id: str = "m2",
    context_cluster_id: str | None = None,
    cookbooks: Mapping[str, Cookbook] | None = None,
    context_file: str | None = None,
    service_principal: str = "comet.runner",
    stack_release: StackRelease | None = None,
    gateway_updates: Mapping[str, object] | None = None,
    endpoint_port_updates: Mapping[str, int] | None = None,
    admin_root: Path | None = None,
    public_root: Path | None = None,
) -> DeploymentContext:
    """Return one test-only registered context at a temporary root."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    registry = DeploymentContextRegistry.load(CONTEXTS)
    selected_context_file = context_file or (
        "m1-production.yaml" if cluster_id == "m1" else "m2-development.yaml"
    )
    registered_spec = registry.resolve(
        explicit_path=CONTEXTS / selected_context_file,
        environ={},
    )
    effective_context_cluster_id = (
        registered_spec.cluster_id if context_cluster_id is None else context_cluster_id
    )
    profile = load_cluster_profile(REPO / "clusters" / f"{cluster_id}.yaml")
    selected_cookbooks = {
        model: (
            cookbooks[model]
            if cookbooks is not None and model in cookbooks
            else load_cookbook(REPO / "cookbooks" / f"{model}.yaml")
        )
        for model in models
    }
    template = next(iter(registered_spec.policy.models.values()))
    model_policies = {}
    for model, cookbook in selected_cookbooks.items():
        nnodes = cookbook.parallelism.nnodes or 1
        slots = (
            cookbook.node_shape.gpus_per_node * nnodes
            if cookbook.node_shape.replicas_per_node > 1
            else profile.capacity_slots_per_node * nnodes
        )
        model_policies[model] = template.model_copy(
            update={
                "slots_per_replica": slots,
                "autoscale_min_replicas": None,
                "autoscale_max_replicas": None,
            }
        )
    gateway = registered_spec.policy.gateway.model_copy(update=dict(gateway_updates or {}))
    endpoint = registered_spec.endpoint.model_copy(
        update={
            "ports": registered_spec.endpoint.ports.model_copy(
                update=dict(endpoint_port_updates or {})
            )
        }
    )
    policy = registered_spec.policy.model_copy(
        update={
            "models": model_policies,
            "gateway": gateway,
            "autoscale": (
                registered_spec.policy.autoscale.model_copy(update={"enabled": False})
                if context_cluster_id is not None
                else registered_spec.policy.autoscale
            ),
        }
    )
    public_cluster_root = public_root or root / "public-cluster"
    selected_admin_root = admin_root or Path(
        "/lustrefs/shared/comet" if cluster_id == "m1" else "/mnt/weka/shrd/comet"
    )
    admin_deployment = selected_admin_root.joinpath(*registered_spec.deployment_suffix.parts)
    spec = registered_spec.model_copy(
        update={
            "cluster_id": effective_context_cluster_id,
            "cluster_root": public_cluster_root,
            "credentials": registered_spec.credentials.model_copy(
                update={
                    "human_scope": f"cluster:{effective_context_cluster_id}",
                    "machine_role": service_principal,
                }
            ),
            "owner": service_principal,
            "service_principal": service_principal,
            "policy": policy,
            "endpoint": endpoint,
        }
    )
    if context_cluster_id is not None:
        spec = DeploymentContextSpec.model_validate(spec.model_dump(mode="python"))
    if stack_release is None:
        pools = {}
        for model in models:
            cookbook = selected_cookbooks[model]
            resolved = cookbook.for_cluster(cluster_id)
            image_sha256 = resolved.image_sha256 or (
                hashlib.sha256(model.encode()).hexdigest() if resolved.image is not None else None
            )
            pools[model] = {
                "description": model,
                "provenance": pool_runtime_provenance(
                    cookbook,
                    profile,
                    spec,
                    admin_root=selected_admin_root,
                    image_sha256=image_sha256,
                ),
            }
        stack_release = StackRelease.model_validate(
            {
                "version": "0.1.0",
                "clusters": {
                    effective_context_cluster_id: {
                        "control": {
                            "description": "control",
                            "provenance": {"revision": "a" * 40},
                        },
                        "profile": {"description": "profile", "provenance": {"sha256": "p"}},
                        "smg": {
                            "description": "smg",
                            "provenance": {
                                "image_path": str(selected_admin_root / "images" / "smg-test.sqsh"),
                                "image_sha256": "1" * 64,
                            },
                        },
                        "pools": pools,
                    }
                },
            }
        )
    locator_sha256 = hashlib.sha256(
        f"{public_cluster_root}\0{selected_admin_root}".encode()
    ).hexdigest()
    public_deployment = public_cluster_root.joinpath(*spec.deployment_suffix.parts)
    public_deployment.mkdir(parents=True, exist_ok=True, mode=0o755)
    public_deployment.chmod(0o755)
    roots = ResolvedRootIdentity(
        public_root=public_cluster_root,
        admin_root=selected_admin_root,
        public_deployment=public_deployment,
        admin_deployment=admin_deployment,
        locator_sha256=locator_sha256,
        public_device=1,
        public_inode=1,
        admin_device=2,
        admin_inode=2,
        public_deployment_device=3,
        public_deployment_inode=3,
        admin_deployment_device=4,
        admin_deployment_inode=4,
    )
    return DeploymentContext._from_registered(
        _RegisteredContext(root / selected_context_file, spec),
        stack_release,
        roots=roots,
        test_root_override=root,
    )


def admitted_test_profile(cluster_id: str):
    """Return one test profile with an admitted physical scheduler identity."""
    profile = load_cluster_profile(REPO / "clusters" / f"{cluster_id}.yaml")
    identity = profile.scheduler_identity
    scheduler_cluster_id = identity.scheduler_cluster_id or f"{cluster_id}-scheduler"
    return profile.model_copy(
        update={
            "scheduler_identity": SchedulerIdentityProfileV1(
                enabled=True,
                scheduler_cluster_id=scheduler_cluster_id,
                adapter="slurm-local-v1",
                generation_scheme="submit-start-restart-v1",
                disabled_reason=None,
            )
        }
    )


def make_test_runtime(root: Path, **kwargs) -> RuntimeEnvironment:
    """Return one test-only complete runtime at a temporary root."""
    context = make_test_context(root, **kwargs)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    (root / "context-transition.lock").touch(mode=0o600)
    return RuntimeEnvironment(
        context=context,
        paths=CometPaths.from_context(context),
        profile=admitted_test_profile(context.cluster_id),
    )


def activate_test_stack_release(
    release: StackRelease,
    paths: CometPaths,
    *,
    context: DeploymentContext,
) -> None:
    """Install test state without exposing a stack-only production operation."""
    cluster_stack_stamp(release, context=context)
    with context_transition_guard(context, mode="exclusive"):
        _activate_stack_release_locked(release, paths, context=context)


def make_scheduler_observation(
    context: DeploymentContext,
    job_id: str,
    job_name: str,
    *,
    owner_uid: int = 1001,
) -> SlurmObservationV2:
    """Return complete test scheduler evidence for one context-owned job."""
    scheduler_cluster_id = admitted_test_profile(
        context.cluster_id
    ).scheduler_identity.scheduler_cluster_id
    assert scheduler_cluster_id is not None
    return SlurmObservationV2(
        cluster_id=scheduler_cluster_id,
        job_id=job_id,
        job_name=job_name,
        generation=f"test-generation-{job_id}",
        owner_uid=owner_uid,
        owner_user=context.spec.service_principal,
        account="test-account",
        association="test-association",
        partition="test-partition",
        qos=None,
        reservation=None,
        state="RUNNING",
        end_time=None,
        requested_nodes=("test-node",),
        allocated_nodes=("test-node",),
        requested_resources={"gres/gpu": "1"},
        allocated_resources={"gres/gpu": "1"},
        retained_script_sha256="1" * 64,
        context=SchedulerContextEvidenceV2.from_context(context),
    )


def write_test_gateway(
    paths: CometPaths,
    *,
    job_id: str = "1",
    node: str = "gw",
) -> None:
    """Publish complete gateway evidence for a test deployment context."""
    context = paths.context
    assert context is not None
    ports = context.spec.endpoint.ports
    paths.gateway_json.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    paths.gateway_json.parent.chmod(0o750)
    atomic_write_json(
        paths.gateway_json,
        GatewayInfo(
            schema_version=2,
            context_id=context.context_id,
            deployment_id=context.deployment_id,
            release_id=context.release_id,
            source_sha=context.source_sha,
            scheduler_namespace=context.spec.scheduler_namespace,
            endpoint_id=context.spec.endpoint.endpoint_id,
            cluster=context.cluster_id,
            base_url=f"http://{node}:{ports.gateway}",
            node=node,
            port=ports.gateway,
            slurm_job_id=job_id,
            started_at="t0",
            smg_url=f"http://{node}:{ports.smg}",
        ),
        mode=0o640,
    )


def gateway_for_test_context(
    context: DeploymentContext,
    *,
    job_id: str = "1",
    generation: str = "gateway-generation-1",
) -> TestGatewayInfo:
    """Return gateway evidence derived only from one selected context."""
    endpoint = context.spec.endpoint
    origin = f"{endpoint.advertised_scheme}://{endpoint.advertised_hostname}"
    return TestGatewayInfo(
        context_id=context.context_id,
        deployment_id=context.deployment_id,
        release_id=context.release_id,
        source_sha=context.source_sha,
        scheduler_namespace=context.spec.scheduler_namespace,
        endpoint_id=endpoint.endpoint_id,
        cluster=context.cluster_id,
        base_url=f"{origin}:{endpoint.ports.gateway}",
        node=endpoint.advertised_hostname,
        port=endpoint.ports.gateway,
        slurm_job_id=job_id,
        started_at="t0",
        smg_url=f"{origin}:{endpoint.ports.smg}",
        generation=generation,
    )


def published_public_runtime(context: DeploymentContext) -> PublicRuntime:
    """Return public runtime values derived only from one selected context."""
    return PublicRuntime(
        spec=context.spec,
        paths=PublicDeploymentPaths(context.public_root),
        context=PublicContextV1(
            cluster_id=context.cluster_id,
            deployment_id=context.deployment_id,
            context_id=context.context_id,
            release_id=context.release_id,
            source_sha=context.source_sha,
            endpoint_id=context.spec.endpoint.endpoint_id,
        ),
        service_uid=os.geteuid(),
        service_gid=os.getegid(),
    )


def set_machine_credential_env(monkeypatch, context: DeploymentContext, key: str) -> None:
    """Set one complete test machine credential for a context."""
    monkeypatch.setenv("COMET_ENGINE_API_KEY", key)
    monkeypatch.setenv("COMET_ENGINE_API_KEY_CLASS", "local-deployment")
    monkeypatch.setenv("COMET_ENGINE_API_KEY_CLUSTER", context.cluster_id)
    monkeypatch.setenv("COMET_ENGINE_API_KEY_DEPLOYMENT", context.deployment_id)
    monkeypatch.setenv("COMET_ENGINE_API_KEY_ROLE", context.spec.credentials.machine_role)
