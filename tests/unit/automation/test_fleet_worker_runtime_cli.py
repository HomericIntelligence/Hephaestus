"""Drive the actual Fleet CLI with an owned protocol fixture and local socket."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.unit.automation.test_fleet_build_service import BuildConsumerHTTP

pytestmark = pytest.mark.precommit
PROVIDER = Path(__file__).resolve().parents[2] / "fixtures" / "fleet_provider.py"


@dataclass
class OwnedCLI:
    """Keep the exact CLI child and its captured terminal result."""

    process: subprocess.Popen[str]
    provider_record: Path
    stdout: str = ""
    stderr: str = ""
    graceful: bool = False

    def close(self) -> None:
        """Stop only this child and record whether its normal cleanup completed."""
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)
        try:
            self.stdout, self.stderr = self.process.communicate(timeout=10)
            self.graceful = True
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.stdout, self.stderr = self.process.communicate(timeout=5)
        if self.provider_record.exists():
            provider = json.loads(self.provider_record.read_text())
            assert provider["parent"] == self.process.pid
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    # Signal zero observes existence; it cannot terminate a reused PID.
                    os.kill(provider["pid"], 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                raise AssertionError("the owned protocol fixture did not exit")


class WorkerCLI:
    """Prepare private paths and invoke the installed CLI entry point."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.codex = root / "codex"
        self.workspaces = root / "workspaces"
        for directory in (self.state, self.codex, self.workspaces):
            directory.mkdir(mode=0o700)
        self.launcher = root / "fixture-codex"
        self.parent_record = root / "cli-parent.json"
        self.provider_record = root / "provider.json"
        python = Path(sys.executable).resolve(strict=True)
        self.launcher.write_text(
            f"#!{python}\n"
            "import json, os, runpy, sys, threading, time\n"
            "from pathlib import Path\n"
            "if 'app-server' in sys.argv:\n"
            f"    parent_record = Path({str(self.parent_record)!r})\n"
            "    deadline = time.monotonic() + 5\n"
            "    while not parent_record.exists():\n"
            "        if time.monotonic() >= deadline:\n"
            "            os._exit(91)\n"
            "        time.sleep(0.01)\n"
            "    parent = json.loads(parent_record.read_text())\n"
            "    if os.getppid() != parent:\n"
            "        os._exit(92)\n"
            f"    Path({str(self.provider_record)!r}).write_text(\n"
            "        json.dumps({'pid': os.getpid(), 'parent': parent}))\n"
            "    def watch_parent():\n"
            "        while os.getppid() == parent:\n"
            "            time.sleep(0.02)\n"
            "        os._exit(0)\n"
            "    threading.Thread(target=watch_parent, daemon=True).start()\n"
            f"runpy.run_path({str(PROVIDER)!r}, run_name='__main__')\n"
        )
        self.launcher.chmod(0o700)
        self.entrypoint = Path(sys.executable).with_name("hephaestus-fleet-worker")
        assert self.entrypoint.is_file(), "the installed CLI entry point is required"

    @contextmanager
    def run(self, *profile: str) -> Iterator[OwnedCLI]:
        """Keep one CLI child until the enclosing behavior observation ends."""
        assert not (self.state / "worker.sock").exists()
        self.parent_record.unlink(missing_ok=True)
        self.provider_record.unlink(missing_ok=True)
        arguments = [
            sys.executable,
            "-B",
            str(self.entrypoint),
            "serve",
            "--state-dir",
            str(self.state),
            "--workspace-root",
            str(self.workspaces),
            "--codex-home",
            str(self.codex),
            "--worker-id",
            "worker-cli",
            "--pool-id",
            "local",
            "--host-id",
            "fixture-host",
            "--capacity",
            "2",
            "--generation",
            "1",
            "--codex-bin",
            str(self.launcher),
            *profile,
        ]
        environment = os.environ.copy()
        environment["AGAMEMNON_API_KEY"] = "fixture-private-key"
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        owned = OwnedCLI(process, self.provider_record)
        try:
            pending_parent = self.parent_record.with_suffix(".tmp")
            pending_parent.write_text(json.dumps(process.pid))
            pending_parent.replace(self.parent_record)
            yield owned
        finally:
            owned.close()

    def inventory(self, child: OwnedCLI) -> dict[str, object] | None:
        """Observe readiness through the actual bounded private socket protocol."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and child.process.poll() is None:
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(0.2)
                    connection.connect(str(self.state / "worker.sock"))
                    connection.sendall(b'{"operation":"inventory"}\n')
                    with connection.makefile("rb") as stream:
                        response = stream.readline(65537)
                if len(response) > 65536 or not response.endswith(b"\n"):
                    raise AssertionError("the worker returned an invalid inventory frame")
                result = json.loads(response)
                assert isinstance(result, dict)
                assert child.provider_record.is_file(), "the fixture identity was not retained"
                return result
            except (FileNotFoundError, ConnectionRefusedError, TimeoutError):
                time.sleep(0.01)
        return None


@pytest.fixture
def qualified_cli() -> Iterator[WorkerCLI]:
    """Prove the unmodified CLI/provider setup before the new-profile assertion."""
    # The real storage guard rejects shared scratch. Use the repository's ignored build root.
    build = Path(__file__).resolve().parents[3] / "build"
    build.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="fleet-runtime-cli-", dir=build) as temporary:
        case = WorkerCLI(Path(temporary))
        version = subprocess.run(
            [str(case.launcher), "--version"], capture_output=True, text=True, timeout=5
        )
        assert version.returncode == 0, version.stderr
        assert version.stdout.strip() == "codex-cli 0.153.4"
        with case.run() as legacy:
            inventory = case.inventory(legacy)
        assert inventory is not None, f"legacy CLI setup failed: {legacy.stderr}"
        assert inventory["workerId"] == "worker-cli"
        assert inventory["sessions"] == []
        assert legacy.graceful and legacy.process.returncode == 0, legacy.stderr
        assert not (case.state / "worker.sock").exists()
        yield case


def test_worker_cli_controller_profile_keeps_the_real_local_attachment(
    qualified_cli: WorkerCLI, request: pytest.FixtureRequest
) -> None:
    """The explicit local controller profile must reach ordinary worker readiness."""
    controller = BuildConsumerHTTP()
    request.addfinalizer(controller.close)
    with qualified_cli.run(
        "--controller-port", str(controller.port), "--controller-timeout", "1"
    ) as worker:
        inventory = qualified_cli.inventory(worker)

    assert inventory is not None, (
        f"controller profile did not start the local worker: "
        f"exit={worker.process.returncode}; stderr={worker.stderr}"
    )
    assert inventory["workerId"] == "worker-cli"
    assert inventory["capacity"] == 2
    assert inventory["sessions"] == []
    assert worker.graceful and worker.process.returncode == 0, worker.stderr
    assert not (qualified_cli.state / "worker.sock").exists()
    records = [
        json.loads(line)
        for line in (qualified_cli.state / "receipts.jsonl").read_text().splitlines()
    ]
    last_runtime = next(record for record in reversed(records) if record["kind"] == "runtime")
    assert last_runtime["value"] == {"pid": None, "uncertain": False}
