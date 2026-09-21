"""Exercise contained CLI startup with real journals, sockets, and fixture processes."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import tomllib
import venv
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hephaestus.automation import fleet_podman, fleet_worker, fleet_worker_cli
from hephaestus.automation.fleet_journal import WorkerJournal
from hephaestus.automation.fleet_provider import CodexAppServer
from tests.unit.automation.test_fleet_attachment import PipeEngine
from tests.unit.automation.test_fleet_containment import Kernel
from tests.unit.automation.test_fleet_worker import command

pytestmark = pytest.mark.precommit
FIXTURE = Path(__file__).parents[2] / "fixtures" / "fleet_provider.py"


class RuntimeEngine:
    """Control separate echo processes at the container engine boundary."""

    def __init__(self) -> None:
        self.children: dict[str, PipeEngine] = {}
        self.closed = False
        self.fail_create = False
        self.acquired = False

    def identity(self) -> dict[str, str]:
        return {"socket": "fixture-engine", "home": "fixture-private-home"}

    def create(self, spec: Any, lease_id: str) -> str:
        if self.fail_create and self.children:
            raise RuntimeError("fixture_second_create_failure")
        child = PipeEngine()
        child.create(spec, lease_id)
        container_id = f"{len(self.children) + 1:064x}"
        cast(dict[str, Any], child.snapshot)["Id"] = container_id
        self.children[container_id] = child
        return container_id

    def inspect(self, container_id: str) -> dict[str, Any]:
        return copy.deepcopy(cast(dict[str, Any], self.children[container_id].snapshot))

    def attach(self, container_id: str) -> subprocess.Popen[bytes]:
        return self.children[container_id].attach(container_id)

    def close(self) -> None:
        for child in self.children.values():
            if child.process is not None:
                if child.process.poll() is None:
                    child.process.terminate()
                child.process.wait(timeout=2)
                for stream in (child.process.stdin, child.process.stdout, child.process.stderr):
                    stream.close()
        self.closed = True


@pytest.fixture
def runtime_setup(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    """Replace external engines and the provider, with the admission guard unchanged."""
    private_storage = getattr(request, "param", False)
    parent = Path.home() / ".cache" / "hf" if private_storage else Path("/tmp")
    if private_storage:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        assert parent.is_dir() and not parent.is_symlink()
        assert parent.stat().st_uid == os.getuid() and parent.stat().st_mode & 0o077 == 0
    capacity = 1 if private_storage else 2
    with tempfile.TemporaryDirectory(prefix="hf-", dir=parent) as directory:
        root = Path(directory).resolve()
        for name in (
            "auth",
            "worker",
            "supervisor",
            "engine",
            "control",
            "gateway",
            "spool",
            "work",
        ):
            (root / name).mkdir(mode=0o700)
        environments = []
        for index in range(1, capacity + 1):
            workspace = root / "work" / str(index)
            workspace.mkdir()
            environments.append(
                {
                    "environmentId": f"env-{index}",
                    "spec": {
                        "workerId": "worker-a",
                        "sessionId": f"session-{index}",
                        "executionId": f"execution-{index}",
                        "generation": 1,
                        "workspace": str(workspace),
                        "imageDigest": "sha256:" + "a" * 64,
                        "cpus": 1,
                        "memoryBytes": 1024**3,
                        "pidsLimit": 64,
                    },
                }
            )
        config = {
            "schema": "hi/fleet/contained-runtime/v1",
            "supervisorState": str(root / "supervisor"),
            "engine": {
                "executable": sys.executable,
                "socket": str(root / "engine" / "engine.sock"),
                "home": str(root / "engine"),
            },
            "attachmentProgram": sys.executable,
            "authorityRoots": {
                "controller": str(root / "control"),
                "gateway": str(root / "gateway"),
                "spool": str(root / "spool"),
            },
            "environments": environments,
        }
        config_path = root / "control" / "runtime.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        engine = RuntimeEngine()
        providers = []

        def provider(_command: list[str], home: Path) -> CodexAppServer:
            value = CodexAppServer([sys.executable, "-u", str(FIXTURE)], home)
            providers.append(value)
            return value

        def acquire_engine(**_: Any) -> RuntimeEngine:
            engine.acquired = True
            return engine

        monkeypatch.setattr(fleet_worker, "CodexAppServer", provider)
        if not private_storage:
            monkeypatch.setattr(fleet_worker, "validate_worker_storage", lambda *_: None)
        monkeypatch.setattr(fleet_podman, "PodmanEngine", acquire_engine)
        monkeypatch.setattr(fleet_podman, "LinuxKernel", lambda: Kernel(PipeEngine()))
        arguments = ["serve", "--contained-config", str(config_path)]
        for name, value in {
            "state-dir": root / "worker",
            "workspace-root": root / "work",
            "codex-home": root / "auth",
            "worker-id": "worker-a",
            "pool-id": "pool-a",
            "host-id": "host-a",
            "capacity": capacity,
            "generation": 1,
        }.items():
            arguments.extend(["--" + name, str(value)])
        yield root, config, config_path, arguments, engine, providers
        engine.close()
        for value in providers:
            value.close()


@pytest.fixture
def contained_session(runtime_setup, monkeypatch: pytest.MonkeyPatch):
    """Prepare the real worker with a provider that uses its owned attachment."""
    from hephaestus.automation.fleet_runtime import ContainedRuntime

    root, _config, path, _arguments, _engine, _providers = runtime_setup
    # Control the host observation, not the production admission decision.
    monkeypatch.setattr(fleet_worker, "sys", SimpleNamespace(platform="linux"))
    original_request = CodexAppServer.request
    requests = []
    expected_selection = [
        {"environmentId": "env-1", "cwd": "/workspace", "runtimeWorkspaceRoots": ["/workspace"]}
    ]
    with ExitStack() as attachments:

        def request(provider, method, params, *, timeout=None):
            if method in {"thread/start", "turn/start"}:
                requests.append((method, copy.deepcopy(params)))
                assert params["environments"] == expected_selection
            if method == "thread/start":
                registry = tomllib.loads((provider.codex_home / "environments.toml").read_text())
                assert registry["include_local"] is False and registry["default"] == "none"
                assert len(registry["environments"]) == 1
                environment = registry["environments"][0]
                assert environment["id"] == "env-1"
                args = environment["args"]
                values = dict(zip(args[2::2], args[3::2], strict=True))
                client = attachments.enter_context(
                    socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                )
                client.settimeout(2)
                client.connect(values["--socket"])
                client.sendall(
                    json.dumps(
                        {
                            "schema": "hi/fleet/attachment/v1",
                            "leaseId": values["--lease-id"],
                            "bindingDigest": values["--binding-digest"],
                        }
                    ).encode()
                    + b"\n"
                )
                stream = attachments.enter_context(client.makefile("rb"))
                assert json.loads(stream.readline(1024)) == {"status": "attached"}
                client.sendall(b"owned attachment marker\n")
                assert stream.readline(1024) == b"owned attachment marker\n"
            return original_request(provider, method, params, timeout=timeout)

        monkeypatch.setattr(CodexAppServer, "request", request)
        worker = fleet_worker.FleetWorker(
            state_dir=root / "worker",
            workspace_root=root / "work",
            codex_home=root / "auth",
            worker_id="worker-a",
            pool_id="pool-a",
            host_id="host-a",
            generation=1,
            capacity=1,
        )
        runtime = ContainedRuntime(worker, path)
        try:
            runtime.start()
            start = command(
                "start",
                payload={
                    "workspace": "1",
                    "agentId": "agent-1",
                    "taskId": "task-1",
                    "executionId": "execution-1",
                    "stage": "implementation",
                    "issueRefs": ["HomericIntelligence/Hephaestus#3308"],
                },
            )
            yield worker, runtime, requests, start, original_request
        finally:
            attachments.close()
            runtime.close()


@pytest.mark.parametrize("runtime_setup", [True], indirect=True, ids=["private-storage"])
def test_contained_runtime_admits_one_fresh_session(contained_session) -> None:
    """Admit work only through the real runtime and its owned attachment."""
    worker, runtime, requests, start, _original_request = contained_session
    result = worker.handle(start)
    assert result["status"] == "completed", (
        f"start error: {result.get('error') or result.get('receipt', {}).get('error')}"
    )
    assert worker.handle(start) == result
    assert [method for method, _ in requests] == ["thread/start"]
    session = worker.inventory()["sessions"][0]
    assert session["activity"] == "idle" and session["providerTurnId"] is None
    assert session["providerThreadId"] == result["receipt"]["providerThreadId"]
    assert worker.journal.commands[start["idempotencyKey"]]["result"] == result
    assert runtime.supervisor is not None
    lease = runtime.supervisor.inventory()[0]
    assert lease["phase"] == "active" and lease["spec"]["sessionId"] == session["sessionId"]
    assert lease["runtime"]["containerId"] == lease["containerId"]
    assert requests[0][1]["config"]["permissions"]["fleet"] == {
        "filesystem": {":minimal": "read", "/workspace": "write"},
        "network": {"enabled": False},
    }
    result = worker.handle(command("input", number=2, payload={"text": "approval"}))
    assert result["status"] == "completed", result
    # This response follows the fixture's approval frame on the same stream.
    worker.provider.request("fixture/last-request", {"method": "turn/start"})
    worker.poll()
    assert worker.inventory()["sessions"][0]["activity"] == "waiting_approval"
    response = command(
        "respond",
        number=3,
        payload={"requestId": "approve-1", "response": {"decision": "decline"}},
    )
    result = worker.handle(response)
    assert result["status"] == "completed", result
    assert worker.handle(response) == result
    worker.provider.request("fixture/last-request", {"method": "turn/start"})
    session = worker.inventory()["sessions"][0]
    assert session["activity"] == "idle" and session["outcome"] == "completed"
    assert [method for method, _ in requests] == ["thread/start", "turn/start"]
    assert worker.inventory()["activeReservations"] == 1
    resumed = worker.handle(command("resume", number=4))
    assert resumed["receipt"]["error"] == "environment_resume_requires_reconciliation"
    assert worker.provider.request("fixture/last-request", {"method": "thread/resume"}) == {}


@pytest.mark.parametrize("runtime_setup", [True], indirect=True, ids=["private-storage"])
@pytest.mark.parametrize(
    "failure,expected",
    [
        ("macos", "native_macos_requires_isolated_linux_worker"),
        ("bare-linux", "linux_execution_requires_verified_boundary"),
        ("capacity", "contained_execution_requires_capacity_one"),
        ("supervisor", "containment_supervisor_required"),
        ("assignment", "environment_not_owned"),
        ("owner", "environment_binding_mismatch"),
        ("registry", "environment_configuration_changed"),
        ("engine", "engine_context_changed"),
    ],
)
def test_contained_start_rejects_unverified_assignment(
    contained_session, monkeypatch: pytest.MonkeyPatch, failure, expected
) -> None:
    """Reject an invalid boundary before a thread or reservation is created."""
    worker, runtime, requests, start, _original_request = contained_session
    owner = runtime.supervisor
    lease = owner.inventory()[0]
    if failure == "macos":
        monkeypatch.setattr(fleet_worker, "sys", SimpleNamespace(platform="darwin"))
    elif failure == "bare-linux":
        worker.environment_registry = None
    elif failure == "capacity":
        worker.capacity = 2
    elif failure == "supervisor":
        worker.containment_supervisor = None
    elif failure == "assignment":
        start["payload"]["executionId"] = "other-execution"
    elif failure == "owner":
        owner.leases[lease["leaseId"]]["spec"]["generation"] = 2
    elif failure == "registry":
        (worker.codex_home / "environments.toml").write_text("include_local = true\n")
    elif failure == "engine":
        monkeypatch.setattr(owner.engine, "identity", lambda: {"socket": "other"})
    result = worker.handle(start)
    assert result["status"] == "failed" and result["receipt"]["error"] == expected
    assert worker.handle(start) == result
    assert requests == [] and worker.inventory()["sessions"] == []
    assert worker.inventory()["activeReservations"] == 0


@pytest.mark.parametrize("runtime_setup", [True], indirect=True, ids=["private-storage"])
@pytest.mark.parametrize("failure", ["no-attachment", "changed-owner"])
def test_contained_start_keeps_known_thread_when_readiness_is_unconfirmed(
    contained_session, monkeypatch: pytest.MonkeyPatch, failure
) -> None:
    """Keep the known thread and reservation without retrying an uncertain start."""
    worker, runtime, requests, start, original_request = contained_session
    owner = runtime.supervisor
    lease_id = owner.inventory()[0]["leaseId"]
    request_with_attachment = CodexAppServer.request

    def request(provider, method, params, *, timeout=None):
        invoke = original_request if failure == "no-attachment" else request_with_attachment
        result = invoke(provider, method, params, timeout=timeout)
        if method == "thread/start" and failure == "changed-owner":
            owner.leases[lease_id]["spec"]["generation"] = 2
        return result

    monkeypatch.setattr(CodexAppServer, "request", request)
    result = worker.handle(start)
    expected = (
        "container_phase_not_ready"
        if failure == "no-attachment"
        else "environment_binding_mismatch"
    )
    assert result["status"] == "failed" and result["receipt"]["error"] == expected
    assert worker.handle(start) == result
    session = worker.inventory()["sessions"][0]
    assert session["providerThreadId"] == "thread-1" and session["activity"] == "unknown"
    assert session["waitingReason"] == expected and session["admissionReserved"] is True
    assert worker.inventory()["activeReservations"] == 1 and session["released"] is False
    assert len(requests) == (0 if failure == "no-attachment" else 1)
    assert worker.handle(command("input", number=2, payload={"text": "tool"}))["status"] == "failed"
    assert worker.provider.request("fixture/last-request", {"method": "turn/start"}) == {}


@pytest.mark.parametrize("runtime_setup", [True], indirect=True, ids=["private-storage"])
@pytest.mark.parametrize("operation", ["input", "steer", "respond"])
@pytest.mark.parametrize("failure", ["registry", "stopped", "kernel", "uncertain", "disposed"])
def test_contained_effect_rechecks_current_boundary(
    contained_session, monkeypatch: pytest.MonkeyPatch, operation, failure
) -> None:
    """A lost boundary rejects new input and replies while keeping the task reserved."""
    worker, runtime, requests, start, _original_request = contained_session
    assert worker.handle(start)["status"] == "completed"
    if operation != "input":
        text = "approval" if operation == "respond" else "tool"
        assert worker.handle(command("input", number=2, payload={"text": text}))["status"] == (
            "completed"
        )
        worker.provider.request("fixture/last-request", {"method": "turn/start"})
        worker.poll()
    owner = runtime.supervisor
    lease = owner.inventory()[0]
    if failure == "registry":
        (worker.codex_home / "environments.toml").write_text("include_local = true\n")
        expected = "environment_configuration_changed"
    elif failure == "stopped":
        owner.engine.children[lease["containerId"]].snapshot["State"]["Running"] = False
        expected = "container_running_state_changed"
    elif failure == "kernel":
        owner.kernel.fail_capture = True
        expected = "container_observation_unavailable"
    else:
        owner.leases[lease["leaseId"]]["phase"] = failure
        expected = "container_phase_not_ready"
    responses = []
    monkeypatch.setattr(worker.provider, "respond", lambda *args: responses.append(args))
    methods_before = [method for method, _ in requests]
    action = (
        command(
            "respond",
            number=3,
            payload={"requestId": "approve-1", "response": {"decision": "decline"}},
        )
        if operation == "respond"
        else command("input", number=3, payload={"text": "tool"})
    )
    result = worker.handle(action)
    assert result["status"] == "failed" and result["receipt"]["error"] == expected
    assert worker.handle(action) == result
    assert [method for method, _ in requests] == methods_before and responses == []
    assert worker.provider.request("fixture/last-request", {"method": "turn/steer"}) == {}
    session = worker.inventory()["sessions"][0]
    assert session["activity"] == "unknown" and session["waitingReason"] == expected
    assert session["providerThreadId"] == "thread-1" and session["admissionReserved"] is True
    assert worker.inventory()["activeReservations"] == 1


def test_cli_serves_two_fixed_attachments_with_one_provider(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All attachment loops and remote configuration precede shared provider startup."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    observed = []
    original_start = CodexAppServer.start

    def observe_start(provider: CodexAppServer) -> None:
        config = tomllib.loads((root / "auth" / "environments.toml").read_text())
        assert config["include_local"] is False
        assert config["default"] == "none"
        assert len(config["environments"]) == 2
        for environment in config["environments"]:
            args = environment["args"]
            values = dict(zip(args[2::2], args[3::2], strict=True))
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(values["--socket"])
                client.sendall(
                    json.dumps(
                        {
                            "schema": "hi/fleet/attachment/v1",
                            "leaseId": values["--lease-id"],
                            "bindingDigest": values["--binding-digest"],
                        }
                    ).encode()
                    + b"\n"
                )
                assert client.recv(1024) == b'{"status":"attached"}\n'
                client.sendall(b"fixture-echo\n")
                assert client.recv(1024) == b"fixture-echo\n"
            observed.append(environment["id"])
        original_start(provider)

    def serve(worker: fleet_worker.FleetWorker) -> None:
        assert observed == ["env-1", "env-2"]
        assert worker.journal.sessions == {}
        assert worker.journal.commands == {}
        assert worker.containment_supervisor is not None
        assert len(worker.containment_supervisor.inventory()) == 2
        assert worker.provider.request("fixture/last-request", {"method": "thread/start"}) == {}
        with pytest.raises(ValueError):
            worker.execution_guard({}, False)

    monkeypatch.setattr(CodexAppServer, "start", observe_start)
    monkeypatch.setattr(fleet_worker_cli, "serve", serve)
    assert fleet_worker_cli.main(arguments) == 0
    assert len(providers) == 1
    assert engine.closed
    assert all(child.process.poll() is not None for child in engine.children.values())
    assert list((root / "supervisor").glob("*.sock")) == []
    with_journal = WorkerJournal(root / "supervisor")
    try:
        leases = [
            record["value"] for record in with_journal.records if record["kind"] == "containment"
        ]
        assert len({item["leaseId"] for item in leases}) == 2
        assert all(item["phase"] != "disposed" for item in leases)
        assert not any(item.get("disposal") for item in leases)
    finally:
        with_journal.close()


def test_registry_program_keeps_its_virtual_environment(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The serialized interpreter must load packages from its configured environment."""
    root, config, path, arguments, _engine, providers = runtime_setup
    environment = root / "attachment-venv"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    program = environment / "bin" / "python"
    assert program.is_symlink()
    packages = Path(
        sysconfig.get_path("purelib", vars={"base": str(environment), "platbase": str(environment)})
    )
    (packages / "fleet_attachment_venv_marker.py").write_text('VALUE = "configured-venv"\n')
    config["attachmentProgram"] = str(program)
    path.write_text(json.dumps(config))
    outside = root / "outside"
    outside.mkdir()

    def inspect_registry(_provider: CodexAppServer) -> None:
        registry = tomllib.loads((root / "auth" / "environments.toml").read_text())
        registered_program = registry["environments"][0]["program"]
        result = subprocess.run(
            [
                registered_program,
                "-I",
                "-c",
                "import fleet_attachment_venv_marker as marker; print(marker.VALUE)",
            ],
            cwd=outside,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "configured-venv"
        raise RuntimeError("fixture_registry_program_verified")

    monkeypatch.setattr(CodexAppServer, "start", inspect_registry)
    with pytest.raises(RuntimeError, match="fixture_registry_program_verified"):
        fleet_worker_cli.main(arguments)
    assert providers and all(provider.process is None for provider in providers)


def test_second_creation_failure_releases_local_owners_without_disposal(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial startup retains both creation facts without launching a provider."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    engine.fail_create = True
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises(RuntimeError, match="fixture_second_create_failure"):
        fleet_worker_cli.main(arguments)
    assert engine.closed
    assert len(engine.children) == 1
    assert providers and all(value.process is None for value in providers)
    assert list((root / "supervisor").glob("*.sock")) == []
    for name in ("supervisor", "worker"):
        journal = WorkerJournal(root / name)
        try:
            if name == "supervisor":
                phases = [
                    record["value"]["phase"]
                    for record in journal.records
                    if record["kind"] == "containment"
                ]
                assert "created" in phases and "uncertain" in phases
                assert "disposed" not in phases
        finally:
            journal.close()


@pytest.mark.parametrize(
    "invalid",
    [
        "worker",
        "generation",
        "session",
        "execution",
        "workspace",
        "environment",
        "authority",
        "program-directory",
        "program-not-executable",
        "workspace-comma",
    ],
)
def test_invalid_inventory_fails_before_engine_acquisition(
    runtime_setup, monkeypatch: pytest.MonkeyPatch, invalid: str
) -> None:
    """Reject the whole inventory before the first local engine effect."""
    root, config, path, arguments, engine, providers = runtime_setup
    first, second = config["environments"]
    if invalid == "worker":
        second["spec"]["workerId"] = "other-worker"
    elif invalid == "generation":
        second["spec"]["generation"] = 2
    elif invalid in {"session", "execution", "workspace"}:
        key = invalid if invalid == "workspace" else invalid + "Id"
        second["spec"][key] = first["spec"][key]
    elif invalid == "environment":
        second["environmentId"] = first["environmentId"]
    elif invalid == "authority":
        del config["authorityRoots"]["gateway"]
    elif invalid == "program-directory":
        config["attachmentProgram"] = str(root / "control")
    elif invalid == "program-not-executable":
        config["attachmentProgram"] = str(path)
    else:
        workspace = root / "work" / "comma,name"
        workspace.mkdir()
        second["spec"]["workspace"] = str(workspace)
    path.write_text(json.dumps(config))
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises(ValueError):
        fleet_worker_cli.main(arguments)
    assert engine.acquired is False
    assert engine.children == {}
    assert providers and all(value.process is None for value in providers)


@pytest.mark.parametrize("overlap", ["invocation", "target"])
def test_attachment_program_cannot_overlap_a_workspace(
    runtime_setup, monkeypatch: pytest.MonkeyPatch, overlap: str
) -> None:
    """Keep the invocation path and its resolved executable outside workspaces."""
    root, config, path, arguments, engine, providers = runtime_setup
    workspace_program = root / "work" / "1" / "python"
    if overlap == "invocation":
        workspace_program.symlink_to(sys.executable)
        program = workspace_program
    else:
        shutil.copyfile(sys.executable, workspace_program)
        workspace_program.chmod(0o700)
        program = root / "python"
        program.symlink_to(workspace_program)
    config["attachmentProgram"] = str(program)
    path.write_text(json.dumps(config))
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises(ValueError, match="runtime_authority_overlap"):
        fleet_worker_cli.main(arguments)
    assert engine.acquired is False
    assert providers and all(provider.process is None for provider in providers)


def test_retained_registry_without_leases_cannot_create_containers(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A known incompatible configuration must stop before container creation."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    retained = root / "auth" / "environments.toml"
    retained.write_text('include_local = false\ndefault = "none"\n')
    retained.chmod(0o600)
    before = retained.read_bytes()
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises((ValueError, RuntimeError)):
        fleet_worker_cli.main(arguments)
    assert engine.children == {}
    assert retained.read_bytes() == before
    assert providers and all(value.process is None for value in providers)


def test_restart_reuses_created_leases_and_exact_registry(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unused fixed inventory can restart without new container identities."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    observed = []

    def inspect(worker: fleet_worker.FleetWorker) -> None:
        assert worker.containment_supervisor is not None
        observed.append(worker.containment_supervisor.inventory())

    monkeypatch.setattr(fleet_worker_cli, "serve", inspect)
    assert fleet_worker_cli.main(arguments) == 0
    registry = (root / "auth" / "environments.toml").read_bytes()
    assert fleet_worker_cli.main(arguments) == 0
    assert len(providers) == 2
    assert len(engine.children) == 2
    assert observed[0] == observed[1]
    assert {item["phase"] for item in observed[1]} == {"created"}
    assert (root / "auth" / "environments.toml").read_bytes() == registry


def test_endpoint_listen_failure_removes_only_its_owned_socket(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A constructor failure after bind must not leave an orphan attachment path."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    original = socket.socket.listen
    calls = 0

    def fail_second(channel: socket.socket, backlog: int = 0) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("fixture_listen_failure")
        original(channel, backlog)

    monkeypatch.setattr(socket.socket, "listen", fail_second)
    with pytest.raises(OSError, match="fixture_listen_failure"):
        fleet_worker_cli.main(arguments)
    assert engine.closed
    assert providers and all(value.process is None for value in providers)
    assert list((root / "supervisor").glob("*.sock")) == []


def test_uncertain_engine_cleanup_keeps_supervisor_writer_for_retry(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Release the supervisor writer only after local attachment cleanup succeeds."""
    from hephaestus.automation.fleet_runtime import ContainedRuntime

    root, _config, path, _arguments, engine, _providers = runtime_setup
    worker = fleet_worker.FleetWorker(
        state_dir=root / "worker",
        workspace_root=root / "work",
        codex_home=root / "auth",
        worker_id="worker-a",
        pool_id="pool-a",
        host_id="host-a",
        generation=1,
        capacity=2,
    )
    runtime = ContainedRuntime(worker, path)
    original = engine.close

    def fail_close() -> None:
        raise OSError("fixture_attachment_cleanup_uncertain")

    try:
        runtime.start()
        monkeypatch.setattr(engine, "close", fail_close)
        with pytest.raises(BaseExceptionGroup):
            runtime.close()
        try:
            probe = WorkerJournal(root / "supervisor")
        except RuntimeError:
            locked = True
        else:
            probe.close()
            locked = False
        assert locked, "Unconfirmed attachment cleanup released the supervisor writer."
    finally:
        monkeypatch.setattr(engine, "close", original)
        runtime.close()
    assert engine.closed
    probe = WorkerJournal(root / "supervisor")
    probe.close()


@pytest.mark.parametrize("prior", ["generation", "uncertain", "pid"])
def test_worker_preflight_precedes_engine_acquisition(
    runtime_setup, monkeypatch: pytest.MonkeyPatch, prior: str
) -> None:
    """Retained worker ownership blocks resource preparation without receipt changes."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    journal = WorkerJournal(root / "worker")
    if prior == "generation":
        journal.append("generation", {"generation": 2})
    else:
        journal.append(
            "runtime",
            {"pid": os.getpid() if prior == "pid" else None, "uncertain": prior == "uncertain"},
        )
    journal.close()
    receipts = root / "worker" / "receipts.jsonl"
    before = receipts.read_bytes()
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises(RuntimeError):
        fleet_worker_cli.main(arguments)
    assert not engine.acquired
    assert all(value.process is None for value in providers)
    assert receipts.read_bytes() == before


@pytest.mark.parametrize("failure_point", ["provider", "pid", "control"])
def test_partial_runtime_failure_closes_all_local_resources(
    runtime_setup, monkeypatch: pytest.MonkeyPatch, failure_point: str
) -> None:
    """Keep prepared leases after a failed shared provider or control startup."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    original_start = CodexAppServer.start
    original_append = WorkerJournal.append
    processes: list[subprocess.Popen[bytes]] = []

    def start(provider: CodexAppServer) -> None:
        original_start(provider)
        assert provider.process is not None
        processes.append(provider.process)
        if failure_point == "provider":
            raise RuntimeError("fixture_provider_failure")

    def append(journal: WorkerJournal, kind: str, value: dict[str, Any]) -> None:
        if failure_point == "pid" and kind == "runtime" and value.get("pid") is not None:
            raise RuntimeError("fixture_pid_failure")
        original_append(journal, kind, value)

    def serve(_worker: fleet_worker.FleetWorker) -> None:
        assert failure_point == "control"
        raise RuntimeError("fixture_control_failure")

    monkeypatch.setattr(CodexAppServer, "start", start)
    monkeypatch.setattr(WorkerJournal, "append", append)
    monkeypatch.setattr(fleet_worker_cli, "serve", serve)
    with pytest.raises(RuntimeError, match=f"fixture_{failure_point}_failure"):
        fleet_worker_cli.main(arguments)
    assert engine.closed
    assert len(engine.children) == 2
    assert len(processes) == 1
    assert all(process.poll() is not None for process in processes)
    assert all(value.process is None for value in providers)
    assert list((root / "supervisor").glob("*.sock")) == []
    for name in ("worker", "supervisor"):
        journal = WorkerJournal(root / name)
        try:
            assert journal.sessions == {}
            assert journal.runtime_uncertain is False
            assert all(
                record["value"]["phase"] == "created"
                for record in journal.records
                if record["kind"] == "containment" and record["value"]["phase"] != "creating"
            )
        finally:
            journal.close()


def test_failed_worker_construction_releases_its_journal(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep no writer lock when the unstarted provider object cannot be created."""
    root, _config, _path, arguments, engine, _providers = runtime_setup

    def fail_provider(*_args: Any) -> None:
        raise RuntimeError("fixture_provider_construction_failure")

    monkeypatch.setattr(fleet_worker, "CodexAppServer", fail_provider)
    with pytest.raises(RuntimeError, match="fixture_provider_construction_failure") as failed:
        fleet_worker_cli.main(arguments)
    assert failed.value is not None
    assert not engine.acquired
    journal = WorkerJournal(root / "worker")
    journal.close()


def test_failed_attachment_loop_blocks_provider_start(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bound socket with no serving loop cannot qualify startup readiness."""
    from hephaestus.automation.fleet_attachment import AttachmentEndpoint

    root, _config, _path, arguments, engine, providers = runtime_setup

    def fail_server(_endpoint: AttachmentEndpoint) -> None:
        raise RuntimeError("fixture_server_entry_failure")

    monkeypatch.setattr(AttachmentEndpoint, "serve_once", fail_server)
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: pytest.fail("control served"))
    with pytest.raises(BaseExceptionGroup) as failed:
        fleet_worker_cli.main(arguments)
    assert any(str(error) == "fixture_server_entry_failure" for error in failed.value.exceptions)
    assert providers and all(value.process is None for value in providers)
    assert engine.closed
    assert list((root / "supervisor").glob("*.sock")) == []
    journal = WorkerJournal(root / "supervisor")
    journal.close()


def test_shutdown_deadline_keeps_writer_while_a_serving_thread_remains(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bound the join and keep journal ownership until the last thread stops."""
    from hephaestus.automation import fleet_runtime
    from hephaestus.automation.fleet_attachment import AttachmentEndpoint

    root, _config, _path, arguments, engine, _providers = runtime_setup
    release = threading.Event()
    threads = []
    owners = []

    def delay_server(endpoint: AttachmentEndpoint) -> None:
        threads.append(threading.current_thread())
        endpoint.ready.set()
        release.wait(5)

    def serve(worker: fleet_worker.FleetWorker) -> None:
        assert worker.containment_supervisor is not None
        owners.append(worker.containment_supervisor)

    monkeypatch.setattr(AttachmentEndpoint, "serve_once", delay_server)
    monkeypatch.setattr(fleet_runtime, "_JOIN_TIMEOUT", 0.02)
    monkeypatch.setattr(fleet_worker_cli, "serve", serve)
    started = time.monotonic()
    try:
        with pytest.raises(BaseExceptionGroup):
            fleet_worker_cli.main(arguments)
        assert time.monotonic() - started < 2
        assert not engine.closed
        with pytest.raises(RuntimeError, match="another journal writer"):
            WorkerJournal(root / "supervisor")
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=1)
            assert not thread.is_alive()
        engine.close()
        for owner in owners:
            owner.close()


def test_runtime_retains_uncertain_provider_cleanup_after_failed_start(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing the local owner cannot erase a failed provider cleanup observation."""
    root, _config, _path, arguments, engine, _providers = runtime_setup
    original_start = CodexAppServer.start
    original_close = CodexAppServer.close

    def start(provider: CodexAppServer) -> None:
        original_start(provider)
        raise RuntimeError("fixture_initialization_failure")

    def close(provider: CodexAppServer) -> bool:
        assert original_close(provider) is True
        return False

    monkeypatch.setattr(CodexAppServer, "start", start)
    monkeypatch.setattr(CodexAppServer, "close", close)
    with pytest.raises(RuntimeError, match="fixture_initialization_failure"):
        fleet_worker_cli.main(arguments)
    assert engine.closed
    journal = WorkerJournal(root / "worker")
    try:
        assert journal.runtime_uncertain
        assert journal.runtime_pid is None
    finally:
        journal.close()


def test_auth_owner_conflict_preserves_prepared_leases(runtime_setup) -> None:
    """Authentication ownership failure must not erase container preparation."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    with (root / "auth" / "fleet-owner.lock").open("a+b") as owner:
        fcntl.flock(owner.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(RuntimeError, match="another authentication owner"):
            fleet_worker_cli.main(arguments)
    assert engine.closed
    assert len(engine.children) == 2
    assert all(value.process is None for value in providers)
    assert list((root / "supervisor").glob("*.sock")) == []
    journal = WorkerJournal(root / "supervisor")
    try:
        retained = {
            record["value"]["leaseId"]: record["value"]
            for record in journal.records
            if record["kind"] == "containment"
        }
        assert len(retained) == 2
        assert {item["phase"] for item in retained.values()} == {"created"}
    finally:
        journal.close()


def test_changed_retained_registry_stops_before_provider_restart(
    runtime_setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retain configuration and container identities after a registry mismatch."""
    root, _config, _path, arguments, engine, providers = runtime_setup
    monkeypatch.setattr(fleet_worker_cli, "serve", lambda _: None)
    assert fleet_worker_cli.main(arguments) == 0
    registry = root / "auth" / "environments.toml"
    registry.write_bytes(b"# changed\n" + registry.read_bytes())
    changed = registry.read_bytes()
    identities = set(engine.children)
    with pytest.raises(ValueError, match="environment_configuration_changed"):
        fleet_worker_cli.main(arguments)
    assert len(providers) == 2
    assert all(value.process is None for value in providers)
    assert set(engine.children) == identities
    assert registry.read_bytes() == changed
    assert list((root / "supervisor").glob("*.sock")) == []
