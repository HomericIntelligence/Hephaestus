"""Own local contained runtime resources without granting task admission."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hephaestus.automation import fleet_podman
from hephaestus.automation.fleet_attachment import AttachmentEndpoint
from hephaestus.automation.fleet_containment import ContainedExecSupervisor, ContainerSpec
from hephaestus.automation.fleet_environments import EnvironmentLease, EnvironmentRegistry
from hephaestus.automation.fleet_worker import FleetWorker

_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_READY_TIMEOUT = 5.0
_JOIN_TIMEOUT = 45.0


def _path(value: Any) -> Path:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("runtime_path_must_be_absolute")
    return Path(value)


def _private_root(value: Any) -> Path:
    path = _path(value)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("runtime_root_must_be_private")
    return path.resolve(strict=True)


def _close(action: Callable[[], None], failures: list[BaseException]) -> bool:
    try:
        action()
    except BaseException as error:
        failures.append(error)
        return False
    return True


def _environments(
    entries: Any, worker: FleetWorker, protected: tuple[Path, ...]
) -> tuple[tuple[str, ContainerSpec], ...]:
    if not isinstance(entries, list) or not 1 <= len(entries) <= worker.capacity:
        raise ValueError("invalid_environment_capacity")
    environments: list[tuple[str, ContainerSpec]] = []
    for entry in entries:
        identity = entry["environmentId"]
        spec = ContainerSpec.from_document(entry["spec"])
        if any(character in str(spec.workspace) for character in (",", "\n", "\r")):
            raise ValueError("unsupported_container_workspace")
        if (
            not isinstance(identity, str)
            or not _IDENTITY.fullmatch(identity)
            or not _IDENTITY.fullmatch(spec.worker_id)
            or not _IDENTITY.fullmatch(spec.session_id)
            or spec.worker_id != worker.identity["workerId"]
            or spec.generation != worker.generation
            or not spec.workspace.is_relative_to(worker.workspace_root)
            or spec.workspace == worker.workspace_root
        ):
            raise ValueError("runtime_assignment_mismatch")
        if any(
            spec.workspace.is_relative_to(root) or root.is_relative_to(spec.workspace)
            for root in protected
        ):
            raise ValueError("runtime_authority_overlap")
        for other_id, other in environments:
            if (
                identity == other_id
                or spec.session_id == other.session_id
                or spec.execution_id == other.execution_id
                or spec.workspace.is_relative_to(other.workspace)
                or other.workspace.is_relative_to(spec.workspace)
            ):
                raise ValueError("environment_lease_overlap")
        environments.append((identity, spec))
    return tuple(environments)


@dataclass(frozen=True)
class RuntimeConfiguration:
    """Describe one fixed local resource inventory, with no command authority."""

    supervisor_state: Path
    engine_executable: Path
    engine_socket: Path
    engine_home: Path
    attachment_program: Path
    protected_roots: tuple[Path, ...]
    environments: tuple[tuple[str, ContainerSpec], ...]

    @classmethod
    def load(cls, path: Path, worker: FleetWorker) -> RuntimeConfiguration:
        """Validate the complete private inventory before any engine effect."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 1024 * 1024
            ):
                raise ValueError("runtime_configuration_must_be_private")
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("runtime_configuration_limit")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema") != "hi/fleet/contained-runtime/v1":
            raise ValueError("unsupported_runtime_configuration")
        supervisor_state = _private_root(value["supervisorState"])
        engine = value["engine"]
        engine_executable = _path(engine["executable"]).resolve(strict=True)
        engine_socket = _path(engine["socket"])
        engine_home = _private_root(engine["home"])
        program = _path(value["attachmentProgram"])
        program_target = program.resolve(strict=True)
        program_info = program_target.stat()
        if (
            not stat.S_ISREG(program_info.st_mode)
            or not os.access(program_target, os.X_OK)
            or program_info.st_uid not in {0, os.getuid()}
            or program_info.st_mode & 0o022
        ):
            raise ValueError("runtime_attachment_program_untrusted")
        authorities = value["authorityRoots"]
        if not isinstance(authorities, dict) or set(authorities) != {
            "controller",
            "gateway",
            "spool",
        }:
            raise ValueError("runtime_authority_inventory_required")
        protected = (
            worker.codex_home,
            worker.journal.directory.resolve(),
            supervisor_state,
            engine_home,
            engine_socket.parent.resolve(strict=True),
            engine_executable,
            program.parent.resolve(strict=True) / program.name,
            program_target,
            _private_root(str(path.absolute().parent)),
            *(_private_root(root) for root in authorities.values()),
        )
        return cls(
            supervisor_state,
            engine_executable,
            engine_socket,
            engine_home,
            program,
            protected,
            _environments(value["environments"], worker, protected),
        )


class ContainedRuntime:
    """Keep attachment owners alive until the shared worker stops."""

    def __init__(self, worker: FleetWorker, configuration_path: Path) -> None:
        """Retain an unstarted worker without acquiring container resources."""
        self.worker = worker
        self.configuration_path = configuration_path
        self.engine: fleet_podman.PodmanEngine | None = None
        self.supervisor: ContainedExecSupervisor | None = None
        self.endpoints: list[AttachmentEndpoint] = []
        self.threads: list[threading.Thread] = []
        self.failures: list[BaseException] = []
        self._closed = False

    def _retained_leases(self, config: RuntimeConfiguration) -> list[dict[str, Any]]:
        if self.supervisor is None or self.engine is None:
            raise RuntimeError("contained_runtime_not_prepared")
        retained = self.supervisor.inventory()
        registry = self.worker.codex_home / "environments.toml"
        if not retained:
            if registry.exists() or registry.is_symlink():
                raise ValueError("environment_configuration_without_leases")
            return []
        if len(retained) != len(config.environments):
            raise RuntimeError("contained_runtime_restart_requires_reconciliation")
        by_session = {item["spec"]["sessionId"]: item for item in retained}
        ordered = []
        for _identity, spec in config.environments:
            lease = by_session.get(spec.session_id)
            if (
                lease is None
                or lease["phase"] != "created"
                or lease["spec"] != spec.document()
                or lease["engine"] != self.engine.identity()
            ):
                raise RuntimeError("contained_runtime_restart_requires_reconciliation")
            ordered.append(lease)
        return ordered

    def _serve(self, endpoint: AttachmentEndpoint) -> None:
        try:
            endpoint.serve_once()
        except BaseException as error:
            self.failures.append(error)

    def start(self) -> None:
        """Start fixed attachment loops before the one provider runtime."""
        self.worker.preflight()
        config = RuntimeConfiguration.load(self.configuration_path, self.worker)
        self.engine = fleet_podman.PodmanEngine(
            executable=config.engine_executable,
            socket_path=config.engine_socket,
            home=config.engine_home,
        )
        self.supervisor = ContainedExecSupervisor(
            config.supervisor_state,
            self.engine,
            fleet_podman.LinuxKernel(),
            protected_roots=config.protected_roots,
        )
        retained = self._retained_leases(config)
        leases = []
        for index, (identity, spec) in enumerate(config.environments):
            lease = retained[index] if retained else self.supervisor.create(spec)
            endpoint = AttachmentEndpoint(self.supervisor, lease["leaseId"])
            self.endpoints.append(endpoint)
            thread = threading.Thread(target=self._serve, args=(endpoint,), daemon=True)
            self.threads.append(thread)
            thread.start()
            deadline = time.monotonic() + _READY_TIMEOUT
            while not endpoint.ready.wait(0.01):
                if not thread.is_alive() or time.monotonic() >= deadline:
                    raise RuntimeError("attachment_server_not_ready")
            if not thread.is_alive() or self.failures:
                raise RuntimeError("attachment_server_not_ready")
            leases.append(
                EnvironmentLease.from_endpoint(endpoint, identity, config.attachment_program)
            )
        self.worker.environment_registry = EnvironmentRegistry(self.worker.codex_home, leases)
        self.worker.containment_supervisor = self.supervisor
        self.worker.start()

    def close(self) -> None:
        """Close acquired resources and retain all container disposal decisions."""
        if self._closed:
            return
        failures: list[BaseException] = []
        _close(self.worker.close, failures)
        for endpoint in self.endpoints:
            _close(endpoint.close, failures)
        deadline = time.monotonic() + _JOIN_TIMEOUT
        for thread in self.threads:
            if thread.ident is not None:
                thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(thread.is_alive() for thread in self.threads):
            # A serving thread can still append a supervisor receipt. Keep its
            # writer and engine owned until this process stops or close retries.
            failures.append(RuntimeError("attachment_shutdown_requires_reconciliation"))
        else:
            engine_closed = self.engine is None or _close(self.engine.close, failures)
            if engine_closed:
                self._closed = self.supervisor is None or _close(self.supervisor.close, failures)
        failures.extend(self.failures)
        if failures:
            raise BaseExceptionGroup("contained_runtime_cleanup_failed", failures)
