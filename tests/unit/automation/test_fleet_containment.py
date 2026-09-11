"""Check owned container lifecycle at the engine and kernel observation boundaries."""

from __future__ import annotations

import copy
import json

import pytest

pytestmark = pytest.mark.precommit


def specification(tmp_path):
    """Use one admitted workspace and an immutable synthetic image identity."""
    from hephaestus.automation.fleet_containment import ContainerSpec

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return ContainerSpec(
        worker_id="worker-1",
        session_id="session-1",
        execution_id="execution-1",
        generation=1,
        workspace=workspace,
        image_digest="sha256:" + "a" * 64,
        cpus=1,
        memory_bytes=1024**3,
        pids_limit=128,
    )


class Engine:
    """Substitute only the external container engine and preserve its call order."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.snapshot = None
        self.present = False
        self.fail_remove = False
        self.mutate = lambda snapshot: None

    def identity(self):
        return {"socket": "/run/user/1000/podman/podman.sock", "ownerUid": 1000}

    def create(self, spec, lease_id):
        self.calls.append("create")
        self.present = True
        self.snapshot = {
            "Id": "b" * 64,
            "Image": spec.image_digest,
            "Config": {
                "User": "1000:1000",
                "Hostname": "fleet-tool",
                "Entrypoint": ["/opt/codex-bin/codex"],
                "Cmd": ["exec-server", "--listen", "stdio"],
                "Labels": {"hi.fleet.lease": lease_id},
                "Env": [
                    "HOME=/workspace/.fleet-runtime/home",
                    "XDG_CONFIG_HOME=/workspace/.fleet-runtime/xdg/config",
                    "XDG_CACHE_HOME=/workspace/.fleet-runtime/xdg/cache",
                    "XDG_DATA_HOME=/workspace/.fleet-runtime/xdg/data",
                    "TMPDIR=/tmp",
                    "TMP=/tmp",
                    "TEMP=/tmp",
                    "PATH=/usr/bin:/bin",
                    "SHELL=/bin/sh",
                    "LANG=C.UTF-8",
                    "HOSTNAME=fleet-tool",
                ],
            },
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Privileged": False,
                "NetworkMode": "none",
                "PidMode": "private",
                "IpcMode": "private",
                "UsernsMode": "keep-id:uid=1000,gid=1000",
                "CgroupMode": "private",
                "Tmpfs": {"/tmp": "rw,nosuid,nodev,noexec,size=64m,mode=1777"},
                "SecurityOpt": ["no-new-privileges"],
                "CapAdd": [],
                "CapDrop": ["ALL"],
                "RestartPolicy": {"Name": "no"},
                "Memory": spec.memory_bytes,
                "MemorySwap": spec.memory_bytes,
                "NanoCpus": spec.cpus * 10**9,
                "PidsLimit": spec.pids_limit,
                "Devices": [],
                "PortBindings": {},
            },
            "EffectiveCaps": [],
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": str(spec.workspace),
                    "Destination": "/workspace",
                    "RW": True,
                }
            ],
            "State": {
                "Running": False,
                "Pid": 0,
                "Status": "created",
                "StartedAt": "0001-01-01T00:00:00Z",
            },
        }
        self.mutate(self.snapshot)
        return self.snapshot["Id"]

    def inspect(self, container_id):
        self.calls.append("inspect")
        assert container_id == "b" * 64
        return copy.deepcopy(self.snapshot)

    def attach(self, container_id):
        self.calls.append("attach")
        assert self.present and container_id == self.snapshot["Id"]
        self.snapshot["State"].update(Running=True, Pid=123)
        return object()

    def remove(self, container_id):
        self.calls.append("remove")
        assert container_id == "b" * 64
        if self.fail_remove:
            raise OSError("synthetic engine unavailable")
        self.present = False

    def exists(self, container_id):
        self.calls.append("exists")
        assert container_id == "b" * 64
        return self.present


class Kernel:
    """Supply actual-boundary observation outcomes without pretending to run a container."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.empty = True
        self.fail_capture = False
        self.boot = "boot-1"

    def capture(self, container_id, snapshot, spec):
        self.engine.calls.append("capture")
        if self.fail_capture:
            raise OSError("synthetic kernel unavailable")
        return {
            "bootId": self.boot,
            "containerId": container_id,
            "cgroupPath": f"/sys/fs/cgroup/libpod-{container_id}.scope",
            "processes": [{"pid": 123, "startTimeTicks": "456"}],
        }

    def absent(self, observation):
        self.engine.calls.append("observe_absent")
        return self.empty and observation["bootId"] == self.boot


def supervisor(tmp_path, engine=None, kernel=None):
    """Construct the real journal owner with deterministic external boundaries."""
    from hephaestus.automation.fleet_containment import ContainedExecSupervisor

    engine = engine or Engine()
    return ContainedExecSupervisor(tmp_path / "state", engine, kernel or Kernel(engine))


def test_disposal_requires_causal_kernel_absence_and_retains_a_durable_receipt(tmp_path):
    """Never treat an engine remove acknowledgment or process-stream exit as disposal."""
    owner = supervisor(tmp_path)
    spec = specification(tmp_path)
    try:
        lease = owner.create(spec)
        owner.start(lease["leaseId"])
        receipt = owner.dispose(lease["leaseId"])
        assert receipt["phase"] == "disposed"
        assert receipt["disposal"]["confirmed"] is True
        assert receipt["disposal"]["containerId"] == "b" * 64
        assert receipt["spec"]["executionId"] == "execution-1"
        assert owner.engine.calls[-4:] == ["capture", "remove", "exists", "observe_absent"]
        before = receipt["disposal"]["before"]
        assert before["processes"] == [{"pid": 123, "startTimeTicks": "456"}]
    finally:
        owner.close()
    reopened = supervisor(tmp_path)
    try:
        assert reopened.inspect(lease["leaseId"])["disposal"] == receipt["disposal"]
        assert reopened.reconcile(lease["leaseId"])["phase"] == "disposed"
        assert reopened.engine.calls == []
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "failure", ["kernel_present", "kernel_unavailable", "engine_unavailable", "boot_changed"]
)
def test_uncertain_disposal_retains_lease_and_does_not_claim_empty(tmp_path, failure):
    """Keep ownership when either control or independent observation is incomplete."""
    owner = supervisor(tmp_path)
    try:
        lease = owner.create(specification(tmp_path))
        owner.start(lease["leaseId"])
        if failure == "kernel_present":
            owner.kernel.empty = False
        elif failure == "kernel_unavailable":
            owner.kernel.fail_capture = True
        elif failure == "engine_unavailable":
            owner.engine.fail_remove = True
        else:
            original = owner.engine.remove

            def remove_and_change_boot(container_id):
                original(container_id)
                owner.kernel.boot = "boot-2"

            owner.engine.remove = remove_and_change_boot
        result = owner.dispose(lease["leaseId"])
        assert result["phase"] == "uncertain"
        assert result.get("disposal", {}).get("confirmed") is not True
        assert owner.inspect(lease["leaseId"])["containerId"] == "b" * 64
        assert "synthetic" not in json.dumps(result)
    finally:
        owner.close()


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("HostConfig", "Privileged", True),
        ("HostConfig", "ReadonlyRootfs", False),
        ("HostConfig", "NetworkMode", "host"),
        ("HostConfig", "PidMode", "host"),
        ("HostConfig", "SecurityOpt", []),
        ("HostConfig", "Memory", 2 * 1024**3),
        ("HostConfig", "NanoCpus", 2 * 10**9),
        ("HostConfig", "PidsLimit", 0),
        ("Config", "User", "0"),
        ("Config", "Cmd", ["/bin/sh"]),
        ("Config", "Env", ["CODEX_HOME=/authority"]),
    ],
)
def test_created_boundary_mismatch_never_starts_a_process(tmp_path, section, key, value):
    """Inspect effective image, resource, and authority scope before attachment."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot[section].update({key: value})
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
        assert "remove" not in engine.calls
        assert owner.inventory()[0]["phase"] == "uncertain"
    finally:
        owner.close()


def test_cross_owner_inspection_cannot_authorize_container_removal(tmp_path):
    """The full container ID and immutable lease label must both match."""
    owner = supervisor(tmp_path)
    try:
        lease = owner.create(specification(tmp_path))
        owner.engine.snapshot["Config"]["Labels"]["hi.fleet.lease"] = "other-owner"
        result = owner.dispose(lease["leaseId"])
        assert result["phase"] == "uncertain"
        assert "remove" not in owner.engine.calls
    finally:
        owner.close()


def test_restart_reconciles_an_uncertain_remove_without_repeating_it(tmp_path):
    """Use retained causal inventory to confirm a lost disposal response after restart."""
    owner = supervisor(tmp_path)
    engine, kernel = owner.engine, owner.kernel
    try:
        lease = owner.create(specification(tmp_path))
        owner.start(lease["leaseId"])
        kernel.empty = False
        assert owner.dispose(lease["leaseId"])["phase"] == "uncertain"
    finally:
        owner.close()
    engine.calls.clear()
    kernel.empty = True
    reopened = supervisor(tmp_path, engine, kernel)
    try:
        result = reopened.reconcile(lease["leaseId"])
        assert result["phase"] == "disposed"
        assert engine.calls == ["exists", "observe_absent"]
    finally:
        reopened.close()


def test_supervisor_metadata_cannot_open_worker_linux_admission(tmp_path):
    """Lifecycle evidence alone does not establish normal remote model-tool routing."""
    from hephaestus.automation.fleet_isolation import require_execution_platform

    owner = supervisor(tmp_path)
    try:
        owner.create(specification(tmp_path))
        with pytest.raises(ValueError, match="linux_execution_requires_verified_boundary"):
            require_execution_platform("linux")
    finally:
        owner.close()


def test_workspace_cannot_overlap_supervisor_authority(tmp_path):
    """Reject a writable source mount containing the private lease journal."""
    from dataclasses import replace

    owner = supervisor(tmp_path)
    try:
        spec = replace(specification(tmp_path), workspace=tmp_path)
        with pytest.raises(ValueError, match="supervisor_storage_overlap"):
            owner.create(spec)
        assert owner.engine.calls == []
    finally:
        owner.close()


@pytest.mark.parametrize(
    "entry",
    [
        "AGAMEMNON_API_KEY=synthetic-test-only",
        "NATS_CLIENT_TOKEN=synthetic-test-only",
        "LD_PRELOAD=/outside/library.so",
        "HOME=/outside",
        "PATH=/usr/bin:/bin",
        "MALFORMED_ENTRY",
    ],
)
def test_effective_tool_environment_must_be_exact_and_unambiguous(tmp_path, entry):
    """Reject inherited authority, duplicate keys, malformed entries, and changed roots."""
    engine = Engine()
    engine.mutate = lambda snapshot: snapshot["Config"]["Env"].append(entry)
    owner = supervisor(tmp_path, engine)
    try:
        with pytest.raises(ValueError, match="container_policy_mismatch"):
            owner.create(specification(tmp_path))
        assert "attach" not in engine.calls
    finally:
        owner.close()


@pytest.mark.parametrize("observation", [None, {"containerId": "d" * 64}])
def test_invalid_kernel_capture_cannot_authorize_start_or_disposal(tmp_path, observation):
    """An incomplete causal observation must retain ownership and prevent removal."""
    owner = supervisor(tmp_path)
    try:
        lease = owner.create(specification(tmp_path))
        owner.kernel.capture = lambda *args: observation
        with pytest.raises(ValueError, match="kernel_observation_invalid"):
            owner.start(lease["leaseId"])
        assert owner.inspect(lease["leaseId"])["phase"] == "uncertain"
        assert owner.dispose(lease["leaseId"])["phase"] == "uncertain"
        assert "remove" not in owner.engine.calls
    finally:
        owner.close()


@pytest.mark.parametrize("exists,absent", [(None, True), (False, None), (False, "unknown")])
def test_disposal_requires_explicit_boolean_absence(tmp_path, exists, absent):
    """A missing or untyped external observation cannot release a retained lease."""
    owner = supervisor(tmp_path)
    try:
        lease = owner.create(specification(tmp_path))
        owner.start(lease["leaseId"])
        owner.engine.exists = lambda *args: exists
        owner.kernel.absent = lambda *args: absent
        assert owner.dispose(lease["leaseId"])["phase"] == "uncertain"
    finally:
        owner.close()


def test_disposal_preserves_original_process_identities(tmp_path):
    """A later inventory cannot erase a process whose absence still needs confirmation."""
    owner = supervisor(tmp_path)
    try:
        lease = owner.create(specification(tmp_path))
        owner.start(lease["leaseId"])
        capture = owner.kernel.capture

        def current_inventory(*args):
            result = capture(*args)
            result["processes"] = [{"pid": 789, "startTimeTicks": "1000"}]
            return result

        owner.kernel.capture = current_inventory
        result = owner.dispose(lease["leaseId"])
        assert result["disposal"]["before"]["processes"] == [
            {"pid": 123, "startTimeTicks": "456"},
            {"pid": 789, "startTimeTicks": "1000"},
        ]
    finally:
        owner.close()
