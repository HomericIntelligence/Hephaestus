"""Measure a contained exec-server with fixed synthetic input and no model call."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


class ProbeConnection:
    """Exchange bounded JSON lines with one contained exec-server process."""

    def __init__(self, executable: Path, home: Path) -> None:
        """Use an empty private environment and retain bounded failure diagnostics."""
        with contextlib.ExitStack() as resources:
            self.errors = resources.enter_context(tempfile.TemporaryFile())
            self.process = subprocess.Popen(
                [str(executable), "exec-server", "--listen", "stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self.errors,
                env={"HOME": str(home), "PATH": "/usr/bin:/bin", "SHELL": "/bin/sh"},
                start_new_session=True,
                bufsize=0,
            )
            resources.callback(self._stop_process)
            self.selector = resources.enter_context(selectors.DefaultSelector())
            self.selector.register(self.process.stdout, selectors.EVENT_READ)
            self.resources = resources.pop_all()
        self.buffer = b""
        self.sequence = 0
        self.diagnostics = ""

    def _stop_process(self) -> None:
        if self.process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=2)

    def request(self, method: str, params: dict) -> dict:
        """Wait at most four seconds for a matching response."""
        self.sequence += 1
        message = {"id": self.sequence, "method": method, "params": params}
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    break
                part = os.read(self.process.stdout.fileno(), 8192)
                if not part:
                    raise RuntimeError("exec_server_eof")
                self.buffer += part
                if len(self.buffer) > 1024 * 1024:
                    raise RuntimeError("exec_server_frame_limit")
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            response = json.loads(line)
            if response.get("id") != self.sequence:
                continue
            if "error" in response:
                raise ValueError(str(response["error"])[:512])
            return response["result"]
        raise RuntimeError("exec_server_response_deadline")

    def close(self) -> bool:
        """Observe stdio shutdown; force cleanup of the server group if required."""
        self.process.stdin.close()
        try:
            self.process.wait(timeout=2)
            return self.process.returncode == 0
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait(timeout=2)
            return False
        finally:
            try:
                self.process.stdout.close()
                self.errors.seek(0)
                self.diagnostics = self.errors.read(2048).decode(errors="replace")
            finally:
                self.resources.close()


def owned_processes(nonce: str) -> list[tuple[int, str]]:
    """Identify only generated probe children by argument and kernel start time."""
    matches = []
    for process in Path("/proc").iterdir():
        if not process.name.isdecimal():
            continue
        try:
            if nonce.encode() in (process / "cmdline").read_bytes().split(b"\0"):
                started = (process / "stat").read_text().rsplit(") ", 1)[1].split()[19]
                matches.append((int(process.name), started))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return matches


def clean_owned_processes(nonce: str) -> None:
    """Stop only surviving synthetic children and their own process groups."""
    for pid, started in owned_processes(nonce):
        with contextlib.suppress(ProcessLookupError, FileNotFoundError):
            current = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            if current == started and os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)


def start_process(
    connection: ProbeConnection, process_id: str, workspace: Path, argv: list
) -> dict:
    """Request direct execution inside the external container boundary."""
    return connection.request(
        "process/start",
        {
            "processId": process_id,
            "argv": argv,
            "cwd": workspace.as_uri(),
            "env": {"HOME": str(workspace / "home"), "PATH": "/usr/bin:/bin"},
            "envPolicy": {
                "inherit": "none",
                "ignoreDefaultExcludes": False,
                "exclude": [],
                "set": {},
                "includeOnly": [],
            },
            "tty": False,
            "arg0": None,
            "sandbox": None,
        },
    )


def read_exit(connection: ProbeConnection, process_id: str) -> dict:
    """Require an exit observation, not a termination acknowledgment alone."""
    after = None
    for _ in range(4):
        result = connection.request(
            "process/read",
            {
                "processId": process_id,
                "afterSeq": after,
                "maxBytes": 4096,
                "waitMs": 500,
            },
        )
        if result.get("exited"):
            return result
        after = result["nextSeq"]
    raise RuntimeError("process_exit_unconfirmed")


def run_probe(executable: Path, workspace: Path, forbidden: list[Path]) -> dict:
    """Measure only this endpoint; external container disposal is a separate gate."""
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("private_workspace_required")
    home = workspace / "home"
    home.mkdir(mode=0o700)
    nonce = uuid.uuid4().hex
    marker = workspace / "synthetic-marker"
    marker.write_text(nonce)
    connection = ProbeConnection(executable, home)
    observations = {}
    failure = None
    try:
        initialized = connection.request(
            "initialize",
            {
                "clientName": "fleet-no-auth-probe",
                "resumeSessionId": None,
            },
        )
        connection.process.stdin.write(b'{"method":"initialized","params":{}}\n')
        observations["execSessionId"] = initialized["sessionId"]
        data = connection.request("fs/readFile", {"path": marker.as_uri(), "sandbox": None})
        observations["ownMarkerRead"] = base64.b64decode(data["dataBase64"]).decode() == nonce
        forbidden_reads = []
        for path in forbidden:
            try:
                connection.request("fs/readFile", {"path": path.as_uri(), "sandbox": None})
                forbidden_reads.append(True)
            except ValueError:
                forbidden_reads.append(False)
        observations["forbiddenMarkerReads"] = forbidden_reads
        started = start_process(
            connection,
            "fixed-command",
            workspace,
            [
                "/bin/sh",
                "-c",
                'test "$HOME" = "$1" && test -z "${CODEX_HOME+x}" && test "$(cat "$2")" = "$3"',
                "fleet-probe",
                str(home),
                str(marker),
                nonce,
            ],
        )
        observations["processSandboxType"] = started.get("sandboxType")
        observations["fixedCommandExit"] = read_exit(connection, "fixed-command")["exitCode"]
        start_process(connection, "long-command", workspace, ["/bin/sleep", "30"])
        connection.request("process/terminate", {"processId": "long-command"})
        observations["trackedProcessExitObserved"] = read_exit(connection, "long-command")["exited"]
        ready = workspace / "detached-ready"
        start_process(
            connection,
            "detached-parent",
            workspace,
            [
                "/bin/sh",
                "-c",
                'setsid /bin/sh -c \'printf ready > "$1"; sleep 20; : "$0"\' '
                '"$1" "$2" >/dev/null 2>&1 & '
                'count=0; while ! test -f "$2"; do count=$((count+1)); '
                'test "$count" -lt 100 || exit 2; sleep 0.01; done',
                "fleet-probe",
                nonce,
                str(ready),
            ],
        )
        observations["detachedParentExit"] = read_exit(connection, "detached-parent")["exitCode"]
        observations["detachedAfterParentExit"] = len(owned_processes(nonce))
    except (ValueError, RuntimeError, OSError) as error:
        failure = str(error)[:512]
    finally:
        try:
            observations["stdioShutdownObserved"] = connection.close()
            observations["detachedAfterStdioShutdown"] = len(owned_processes(nonce))
        finally:
            clean_owned_processes(nonce)
    if failure is not None:
        return {
            "schema": "hi/fleet/exec-server-probe/v1",
            "passed": False,
            "observations": observations,
            "error": failure,
            "serverStderr": connection.diagnostics,
            "authorizesAdmission": False,
        }
    version = (
        subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            timeout=5,
            check=True,
            env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        )
        .stdout.decode()
        .strip()
    )
    return {
        "schema": "hi/fleet/exec-server-probe/v1",
        "observations": observations,
        "passed": observations.get("ownMarkerRead") is True
        and observations.get("fixedCommandExit") == 0
        and observations.get("trackedProcessExitObserved") is True
        and observations.get("stdioShutdownObserved") is True
        and observations.get("detachedParentExit") == 0
        and observations.get("detachedAfterParentExit", 0) > 0
        and version == "codex-cli 0.153.4"
        and bool(forbidden)
        and not any(observations.get("forbiddenMarkerReads", [True])),
        "authorizesAdmission": False,
        "providerVersion": version,
        "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "containerDisposalConfirmed": False,
        "normalModelToolRouteTested": False,
        "scope": "direct_exec_server_protocol",
    }


def main() -> int:
    """Print a bounded result from the independently executed canary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--forbidden", type=Path, action="append", required=True)
    args = parser.parse_args()
    try:
        result = run_probe(args.codex_bin, args.workspace, args.forbidden)
    except (ValueError, RuntimeError, OSError) as error:
        result = {
            "schema": "hi/fleet/exec-server-probe/v1",
            "passed": False,
            "error": str(error)[:512],
            "authorizesAdmission": False,
        }
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
