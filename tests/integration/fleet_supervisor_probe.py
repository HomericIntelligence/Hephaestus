"""Measure the production contained-exec supervisor with synthetic markers only."""

from __future__ import annotations

import argparse
import base64
import json
import os
import selectors
import time
import uuid
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_containment import ContainedExecSupervisor, ContainerSpec
from hephaestus.automation.fleet_podman import LinuxKernel, PodmanEngine


class Connection:
    """Send fixed bounded no-auth RPCs through the supervisor-owned attachment."""

    def __init__(self, process: Any) -> None:
        """Keep the existing engine attachment and a bounded response buffer."""
        self.process = process
        self.selector = selectors.DefaultSelector()
        self.selector.register(process.stdout, selectors.EVENT_READ)
        self.sequence = 0
        self.buffer = b""

    def request(self, method: str, params: dict) -> dict:
        """Require a matching JSON-RPC response within four seconds."""
        self.sequence += 1
        frame = {"id": self.sequence, "method": method, "params": params}
        self.process.stdin.write((json.dumps(frame) + "\n").encode())
        self.process.stdin.flush()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                chunk = os.read(self.process.stdout.fileno(), 8192)
                if not chunk:
                    raise RuntimeError("exec_server_eof")
                self.buffer += chunk
                if len(self.buffer) > 1024 * 1024:
                    raise RuntimeError("exec_server_response_limit")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            value = json.loads(line)
            if value.get("id") != self.sequence:
                continue
            if "error" in value:
                raise ValueError(str(value["error"])[:512])
            return value["result"]
        raise RuntimeError("exec_server_response_deadline")


def run_probe(root: Path, executable: Path, socket_path: Path, image: str) -> dict:
    """Create one owned endpoint, observe a live detached child, then confirm disposal."""
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    sibling = root / "sibling"
    sibling.mkdir(mode=0o700)
    authority = root / "authority"
    authority.mkdir(mode=0o700)
    nonce = uuid.uuid4().hex
    for directory in (workspace, sibling, authority):
        (directory / "marker").write_text(nonce)
    engine = PodmanEngine(executable, socket_path, root / "engine-home")
    kernel = LinuxKernel()
    owner = ContainedExecSupervisor(root / "state", engine, kernel)
    lease = None
    connection = None
    result: dict[str, Any] = {
        "schema": "hi/fleet/supervisor-probe/v1",
        "passed": False,
        "authorizesAdmission": False,
        "normalModelToolRoute": False,
        "observations": {},
    }
    stage = "create"
    try:
        spec = ContainerSpec(
            "probe-worker", "probe-session", "probe-execution", 1, workspace, image, 1, 1024**3, 128
        )
        lease = owner.create(spec)
        stage = "start"
        connection = Connection(owner.start(lease["leaseId"]))
        stage = "initialize"
        connection.request(
            "initialize", {"clientName": "fleet-supervisor-no-auth-probe", "resumeSessionId": None}
        )
        connection.process.stdin.write(b'{"method":"initialized","params":{}}\n')
        connection.process.stdin.flush()
        stage = "markers"
        data = connection.request(
            "fs/readFile", {"path": "file:///workspace/marker", "sandbox": None}
        )
        own_read = base64.b64decode(data["dataBase64"]).decode() == nonce
        denied = []
        for path in (sibling / "marker", authority / "marker"):
            if path.read_text() != nonce:
                raise ValueError("synthetic_marker_missing")
            try:
                connection.request("fs/readFile", {"path": path.as_uri(), "sandbox": None})
                denied.append(False)
            except ValueError:
                denied.append(True)
        result["observations"].update(ownMarkerRead=own_read, forbiddenMarkerDenied=denied)
        stage = "detached_child"
        connection.request(
            "process/start",
            {
                "processId": "detached-parent",
                "cwd": "file:///workspace",
                "tty": False,
                "arg0": None,
                "sandbox": None,
                "env": {"PATH": "/usr/bin:/bin", "HOME": "/workspace"},
                "envPolicy": {
                    "inherit": "none",
                    "ignoreDefaultExcludes": False,
                    "exclude": [],
                    "set": {},
                    "includeOnly": [],
                },
                "argv": [
                    "/bin/sh",
                    "-c",
                    'setsid /bin/sh -c \'printf ready > "$1"; sleep 45; : "$0"\' '
                    '"$1" /workspace/ready >/dev/null 2>&1 & '
                    "count=0; while ! test -f /workspace/ready; do count=$((count+1)); "
                    'test "$count" -lt 100 || exit 2; sleep 0.01; done',
                    "fleet-probe",
                    nonce,
                ],
            },
        )
        parent_exited = False
        after = None
        for _ in range(4):
            observed = connection.request(
                "process/read",
                {
                    "processId": "detached-parent",
                    "afterSeq": after,
                    "maxBytes": 4096,
                    "waitMs": 500,
                },
            )
            if observed.get("exited"):
                parent_exited = observed.get("exitCode") == 0
                break
            after = observed["nextSeq"]
        before = kernel.capture(lease["containerId"], engine.inspect(lease["containerId"]), spec)
        children = [
            process
            for process in before["processes"]
            if nonce.encode() in Path(f"/proc/{process['pid']}/cmdline").read_bytes().split(b"\0")
        ]
        result["observations"].update(parentExited=parent_exited, liveDetachedChildren=children)
        stage = "dispose"
        started = time.monotonic()
        receipt = owner.dispose(lease["leaseId"])
        result["disposal"] = receipt
        result["observations"]["disposalElapsedSeconds"] = time.monotonic() - started
        result["passed"] = (
            own_read
            and all(denied)
            and parent_exited
            and bool(children)
            and receipt["phase"] == "disposed"
        )
    except (OSError, ValueError, RuntimeError, KeyError) as error:
        result.update(stage=stage, error=type(error).__name__, detail=str(error)[:512])
    finally:
        if lease is not None and owner.inspect(lease["leaseId"])["phase"] != "disposed":
            result["cleanup"] = owner.dispose(lease["leaseId"])
        result["inventory"] = owner.inventory()
        if connection is not None:
            connection.selector.close()
        engine.close()
        owner.close()
    return result


def main() -> int:
    """Require an explicit private root, engine endpoint, and immutable tool image."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    result = run_probe(args.root, args.engine, args.socket, args.image)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
