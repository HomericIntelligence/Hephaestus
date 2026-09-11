"""Own contained exec-server lifecycle without authorizing Fleet model execution."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

from hephaestus.automation.fleet_journal import WorkerJournal

# These paths belong to the container mount namespace, not host temporary storage.
_CONTAINER_SCRATCH = str(PurePosixPath("/") / "tmp")
TOOL_ENVIRONMENT = {
    "HOME": "/workspace/.fleet-runtime/home",
    "XDG_CONFIG_HOME": "/workspace/.fleet-runtime/xdg/config",
    "XDG_CACHE_HOME": "/workspace/.fleet-runtime/xdg/cache",
    "XDG_DATA_HOME": "/workspace/.fleet-runtime/xdg/data",
    "TMPDIR": _CONTAINER_SCRATCH,
    "TMP": _CONTAINER_SCRATCH,
    "TEMP": _CONTAINER_SCRATCH,
    "PATH": "/usr/bin:/bin",
    "SHELL": "/bin/sh",
    "LANG": "C.UTF-8",
    "HOSTNAME": "fleet-tool",
}


def _expected_environment(entries: Any) -> bool:
    if not isinstance(entries, list) or len(entries) != len(TOOL_ENVIRONMENT):
        return False
    values = {}
    for entry in entries:
        if not isinstance(entry, str) or "=" not in entry:
            return False
        name, value = entry.split("=", 1)
        if name in values:
            return False
        values[name] = value
    return values == TOOL_ENVIRONMENT


def _expected_scratch(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {_CONTAINER_SCRATCH}:
        return False
    if not isinstance(value[_CONTAINER_SCRATCH], str):
        return False
    options = value[_CONTAINER_SCRATCH].split(",")
    required = {"rw", "nosuid", "nodev", "noexec", "size=64m", "mode=1777"}
    return (
        len(set(options)) == len(options)
        and required <= set(options)
        and set(options) <= required | {"rprivate", "tmpcopyup"}
    )


def _expected_userns(host: dict[str, Any], config: dict[str, Any]) -> bool:
    selected = "keep-id:uid=1000,gid=1000"
    if host.get("UsernsMode") == selected:
        return True
    if (
        host.get("UsernsMode") != "private"
        or config.get("Annotations", {}).get("io.podman.annotations.userns") != selected
    ):
        return False
    mappings = host.get("IDMappings", {})
    for name in ("UidMap", "GidMap"):
        values = mappings.get(name)
        if (
            not isinstance(values, list)
            or len(values) != 3
            or values[:2] != ["0:1:1000", "1000:0:1"]
            or not isinstance(values[2], str)
            or re.fullmatch(r"1001:1001:[1-9][0-9]{0,9}", values[2]) is None
        ):
            return False
    return True


def _expected_capabilities(snapshot: dict[str, Any], host: dict[str, Any]) -> bool:
    if snapshot.get("EffectiveCaps") == []:
        return True
    # Podman can omit capability data even for running containers. This validates
    # the requested drop policy only; active attachment always needs kernel proof.
    dropped = host.get("CapDrop")
    known_default_caps = {
        "CAP_CHOWN",
        "CAP_DAC_OVERRIDE",
        "CAP_FOWNER",
        "CAP_FSETID",
        "CAP_KILL",
        "CAP_NET_BIND_SERVICE",
        "CAP_SETFCAP",
        "CAP_SETGID",
        "CAP_SETPCAP",
        "CAP_SETUID",
        "CAP_SYS_CHROOT",
    }
    return (
        snapshot.get("EffectiveCaps") is None
        and isinstance(dropped, list)
        and all(isinstance(value, str) for value in dropped)
        and (dropped == ["ALL"] or set(dropped) == known_default_caps)
    )


@dataclass(frozen=True)
class ContainerSpec:
    """Bind an execution to one source mount and a fixed resource budget."""

    worker_id: str
    session_id: str
    execution_id: str
    generation: int
    workspace: Path
    image_digest: str
    cpus: int
    memory_bytes: int
    pids_limit: int

    def __post_init__(self) -> None:
        """Reject ambiguous owners, image tags, and unbounded resource requests."""
        for value in (self.worker_id, self.session_id, self.execution_id):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value):
                raise ValueError("invalid_container_owner")
        for budget, low, high in (
            (self.generation, 1, 2**31 - 1),
            (self.cpus, 1, 72),
            (self.memory_bytes, 64 * 1024**2, 288 * 1024**3),
            (self.pids_limit, 32, 4096),
        ):
            if type(budget) is not int or not low <= budget <= high:
                raise ValueError("invalid_container_budget")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.image_digest):
            raise ValueError("immutable_container_image_required")
        if self.workspace.is_symlink() or not self.workspace.is_dir():
            raise ValueError("invalid_container_workspace")
        object.__setattr__(self, "workspace", self.workspace.resolve(strict=True))

    def document(self) -> dict[str, Any]:
        """Return bounded ownership metadata for private durable storage."""
        return {
            "workerId": self.worker_id,
            "sessionId": self.session_id,
            "executionId": self.execution_id,
            "generation": self.generation,
            "workspace": str(self.workspace),
            "imageDigest": self.image_digest,
            "cpus": self.cpus,
            "memoryBytes": self.memory_bytes,
            "pidsLimit": self.pids_limit,
        }

    @classmethod
    def from_document(cls, value: dict[str, Any]) -> ContainerSpec:
        """Revalidate retained ownership before an engine operation."""
        return cls(
            worker_id=value["workerId"],
            session_id=value["sessionId"],
            execution_id=value["executionId"],
            generation=value["generation"],
            workspace=Path(value["workspace"]),
            image_digest=value["imageDigest"],
            cpus=value["cpus"],
            memory_bytes=value["memoryBytes"],
            pids_limit=value["pidsLimit"],
        )


def validate_container(snapshot: dict[str, Any], lease: dict[str, Any]) -> None:
    """Check actual engine state against the complete owned boundary policy."""
    spec = lease["spec"]
    config = snapshot.get("Config", {})
    host = snapshot.get("HostConfig", {})
    expected = {
        "ReadonlyRootfs": True,
        "Privileged": False,
        "NetworkMode": "none",
        "PidMode": "private",
        "IpcMode": "private",
        "CgroupMode": "private",
        "Memory": spec["memoryBytes"],
        "MemorySwap": spec["memoryBytes"],
        "NanoCpus": spec["cpus"] * 10**9,
        "PidsLimit": spec["pidsLimit"],
    }
    mounts = snapshot.get("Mounts", [])
    checks = [
        all(host.get(key) == value for key, value in expected.items()),
        _expected_userns(host, config),
        snapshot.get("Id") == lease["containerId"],
        snapshot.get("Image") in (spec["imageDigest"], spec["imageDigest"].removeprefix("sha256:")),
        config.get("User") == "1000:1000",
        config.get("Hostname") == TOOL_ENVIRONMENT["HOSTNAME"],
        config.get("Entrypoint") in (["/opt/codex-bin/codex"], "/opt/codex-bin/codex"),
        config.get("Cmd") == ["exec-server", "--listen", "stdio"],
        config.get("Labels", {}).get("hi.fleet.lease") == lease["leaseId"],
        "no-new-privileges" in host.get("SecurityOpt", []),
        not host.get("CapAdd"),
        _expected_capabilities(snapshot, host),
        not host.get("Devices"),
        not host.get("PortBindings"),
        host.get("RestartPolicy", {}).get("Name") == "no",
        _expected_environment(config.get("Env")),
        _expected_scratch(host.get("Tmpfs")),
        len(mounts) == 1,
    ]
    if len(mounts) == 1:
        mount = mounts[0]
        checks.append(
            mount.get("Type") == "bind"
            and mount.get("Source") == spec["workspace"]
            and mount.get("Destination") == "/workspace"
            and mount.get("RW") is True
        )
    if not all(checks):
        raise ValueError("container_policy_mismatch")


class ContainedExecSupervisor:
    """Keep creation, attachment, and causal disposal behind one private writer."""

    def __init__(self, state_dir: Path, engine: Any, kernel: Any) -> None:
        """Load unresolved leases without restarting or removing their containers."""
        self.journal = WorkerJournal(state_dir)
        self.engine = engine
        self.kernel = kernel
        self.leases: dict[str, dict[str, Any]] = {}
        self.attachments: dict[str, Any] = {}
        for record in self.journal.records:
            if record["kind"] == "containment":
                self.leases[record["value"]["leaseId"]] = record["value"]

    def _save(self, lease: dict[str, Any]) -> dict[str, Any]:
        value = json.loads(json.dumps(lease))
        self.journal.append("containment", value)
        self.leases[value["leaseId"]] = value
        return cast(dict[str, Any], json.loads(json.dumps(value)))

    def inspect(self, lease_id: str) -> dict[str, Any]:
        """Return retained metadata without implying an engine observation."""
        return cast(dict[str, Any], json.loads(json.dumps(self.leases[lease_id])))

    def inventory(self) -> list[dict[str, Any]]:
        """Return all retained leases, including uncertain and disposed outcomes."""
        return [self.inspect(lease_id) for lease_id in self.leases]

    def _check_context(self, lease: dict[str, Any]) -> None:
        if self.engine.identity() != lease["engine"]:
            raise ValueError("engine_context_changed")

    def _capture(self, lease: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
        spec = ContainerSpec.from_document(lease["spec"])
        value = self.kernel.capture(lease["containerId"], snapshot, spec)
        if (
            not isinstance(value, dict)
            or value.get("containerId") != lease["containerId"]
            or not isinstance(value.get("bootId"), str)
            or not value["bootId"]
            or not isinstance(value.get("cgroupPath"), str)
            or f"libpod-{lease['containerId']}.scope" not in Path(value["cgroupPath"]).parts
            or not isinstance(value.get("processes"), list)
            or not 1 <= len(value["processes"]) <= 4096
        ):
            raise ValueError("kernel_observation_invalid")
        for process in value["processes"]:
            if (
                not isinstance(process, dict)
                or type(process.get("pid")) is not int
                or process["pid"] <= 0
                or not isinstance(process.get("startTimeTicks"), str)
                or not process["startTimeTicks"].isdecimal()
            ):
                raise ValueError("kernel_observation_invalid")
        return cast(dict[str, Any], value)

    def create(self, spec: ContainerSpec) -> dict[str, Any]:
        """Persist creation intent before requesting an immutable contained endpoint."""
        authority = self.journal.directory.resolve()
        if authority.is_relative_to(spec.workspace) or spec.workspace.is_relative_to(authority):
            raise ValueError("supervisor_storage_overlap")
        active = [item for item in self.leases.values() if item["phase"] != "disposed"]
        if len(active) >= 24:
            raise ValueError("supervisor_capacity")
        for existing in active:
            other = Path(existing["spec"]["workspace"])
            if (
                spec.session_id == existing["spec"]["sessionId"]
                or spec.workspace.is_relative_to(other)
                or other.is_relative_to(spec.workspace)
            ):
                raise ValueError("container_workspace_owned")
        lease = {
            "schema": "hi/fleet/containment/v1",
            "leaseId": uuid.uuid4().hex,
            "spec": spec.document(),
            "engine": self.engine.identity(),
            "phase": "creating",
            "containerId": None,
        }
        self._save(lease)
        try:
            container_id = self.engine.create(spec, lease["leaseId"])
            if not re.fullmatch(r"[0-9a-f]{64}", container_id):
                raise ValueError("invalid_container_identity")
            lease["containerId"] = container_id
            self._save(lease)
            validate_container(self.engine.inspect(container_id), lease)
        except (OSError, ValueError, RuntimeError):
            self._uncertain(lease, "container_creation_unconfirmed")
            raise
        lease["phase"] = "created"
        return self._save(lease)

    def start(self, lease_id: str) -> Any:
        """Attach once and record the actual same-host kernel boundary before returning."""
        lease = self.inspect(lease_id)
        if lease["phase"] != "created":
            raise ValueError("container_start_requires_reconciliation")
        self._check_context(lease)
        validate_container(self.engine.inspect(lease["containerId"]), lease)
        lease["phase"] = "starting"
        self._save(lease)
        try:
            attachment = self.engine.attach(lease["containerId"])
            self.attachments[lease_id] = attachment
            deadline = time.monotonic() + 5
            while True:
                snapshot = self.engine.inspect(lease["containerId"])
                validate_container(snapshot, lease)
                if snapshot["State"].get("Running"):
                    break
                if time.monotonic() >= deadline:
                    raise ValueError("container_start_unconfirmed")
                time.sleep(0.05)
            lease["runtime"] = self._capture(lease, snapshot)
        except (OSError, ValueError, RuntimeError):
            self._uncertain(lease, "container_start_unconfirmed")
            raise
        lease["phase"] = "active"
        self._save(lease)
        return attachment

    def _uncertain(self, lease: dict[str, Any], reason: str) -> dict[str, Any]:
        lease["phase"] = "uncertain"
        lease["waitingReason"] = reason
        lease.pop("disposal", None)
        return self._save(lease)

    def _confirm_disposal(self, lease: dict[str, Any]) -> dict[str, Any]:
        before = lease.get("disposalBefore")
        if (
            not before
            or self.engine.exists(lease["containerId"]) is not False
            or self.kernel.absent(before) is not True
        ):
            return self._uncertain(lease, "container_disposal_unconfirmed")
        disposal = {
            "confirmed": True,
            "containerId": lease["containerId"],
            "leaseId": lease["leaseId"],
            "spec": lease["spec"],
            "before": before,
            "observedAt": datetime.now(UTC).isoformat(),
        }
        disposal["digest"] = hashlib.sha256(
            json.dumps(disposal, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lease["disposal"] = disposal
        lease["phase"] = "disposed"
        lease.pop("waitingReason", None)
        return self._save(lease)

    def dispose(self, lease_id: str) -> dict[str, Any]:
        """Remove only the owned container and independently observe process/cgroup absence."""
        lease = self.inspect(lease_id)
        if lease["phase"] == "disposed":
            return lease
        try:
            self._check_context(lease)
            snapshot = self.engine.inspect(lease["containerId"])
            validate_container(snapshot, lease)
            current = self._capture(lease, snapshot)
            previous = lease.get("runtime")
            if previous is not None:
                for key in ("bootId", "containerId", "cgroupPath"):
                    if previous[key] != current[key]:
                        raise ValueError("kernel_observation_changed")
                processes = {
                    (item["pid"], item["startTimeTicks"]): item
                    for item in [*previous["processes"], *current["processes"]]
                }
                current["processes"] = [processes[key] for key in sorted(processes)]
            lease["disposalBefore"] = current
            lease["phase"] = "disposing"
            self._save(lease)
            self.engine.remove(lease["containerId"])
            return self._confirm_disposal(lease)
        except (OSError, ValueError, RuntimeError):
            return self._uncertain(lease, "container_disposal_unconfirmed")

    def reconcile(self, lease_id: str) -> dict[str, Any]:
        """Observe a retained lease after restart without repeating create/start/remove."""
        lease = self.inspect(lease_id)
        if lease["phase"] == "disposed":
            return lease
        try:
            self._check_context(lease)
            if lease.get("disposalBefore"):
                return self._confirm_disposal(lease)
            snapshot = self.engine.inspect(lease["containerId"])
            validate_container(snapshot, lease)
            if lease["phase"] == "created" and not snapshot["State"].get("Running"):
                return lease
        except (OSError, ValueError, RuntimeError):
            pass
        return self._uncertain(lease, "container_restart_requires_reconciliation")

    def close(self) -> None:
        """Release the writer without treating attachment detachment as container disposal."""
        self.journal.close()
