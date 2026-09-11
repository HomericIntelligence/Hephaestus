"""Control one explicit Podman context and observe its same-host Linux boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_containment import TOOL_ENVIRONMENT, ContainerSpec

_PROC = Path("/proc")
_CGROUP = Path("/sys/fs/cgroup")
_CID = re.compile(r"[0-9a-f]{64}")


def _container_id(value: Any) -> str:
    if not isinstance(value, str) or not _CID.fullmatch(value):
        raise ValueError("invalid_container_identity")
    return value


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("engine_directory_not_private")
    return path.resolve(strict=True)


class PodmanEngine:
    """Use a fixed executable, private environment, and explicit owned Unix socket."""

    def __init__(self, executable: Path, socket_path: Path, home: Path) -> None:
        """Reject implicit engine discovery and shared control storage."""
        if not executable.is_absolute() or not socket_path.is_absolute():
            raise ValueError("explicit_engine_context_required")
        self.executable = executable.resolve(strict=True)
        self.socket_path = socket_path
        self.home = _private_directory(home)
        self.environment = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            "XDG_CONFIG_HOME": str(_private_directory(self.home / "config")),
            "XDG_DATA_HOME": str(_private_directory(self.home / "data")),
            "XDG_RUNTIME_DIR": str(_private_directory(self.home / "run")),
        }
        self.attachments: list[subprocess.Popen[bytes]] = []
        self.identity()

    def identity(self) -> dict[str, Any]:
        """Bind the control endpoint and executable bytes to retained ownership."""
        executable = self.executable.stat()
        endpoint = self.socket_path.lstat()
        if (
            not stat.S_ISREG(executable.st_mode)
            or executable.st_uid not in (0, os.getuid())
            or executable.st_mode & 0o022
            or not os.access(self.executable, os.X_OK)
            or not stat.S_ISSOCK(endpoint.st_mode)
            or endpoint.st_uid != os.getuid()
            or endpoint.st_mode & 0o007
        ):
            raise ValueError("engine_context_untrusted")
        private_parent = any(
            parent.stat().st_uid == os.getuid() and not parent.stat().st_mode & 0o077
            for parent in self.socket_path.parents
        )
        if not private_parent:
            raise ValueError("engine_context_untrusted")
        return {
            "executable": str(self.executable),
            "executableSha256": hashlib.sha256(self.executable.read_bytes()).hexdigest(),
            "socket": str(self.socket_path),
            "socketDevice": endpoint.st_dev,
            "socketInode": endpoint.st_ino,
            "ownerUid": endpoint.st_uid,
            "home": str(self.home),
        }

    def _argv(self, *arguments: str) -> list[str]:
        return [
            str(self.executable),
            "--remote",
            "--url",
            "unix://" + str(self.socket_path),
            *arguments,
        ]

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        try:
            result = subprocess.run(
                self._argv(*arguments),
                env=self.environment,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("engine_command_timeout") from error
        if len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            raise RuntimeError("engine_response_limit")
        return result

    def create(self, spec: ContainerSpec, lease_id: str) -> str:
        """Create an inert exec-server endpoint with no inherited authority or network."""
        if not re.fullmatch(r"[0-9a-f]{32}", lease_id):
            raise ValueError("invalid_container_lease")
        for protected in (self.home, self.socket_path.resolve(strict=True), self.executable):
            if spec.workspace.is_relative_to(protected) or protected.is_relative_to(spec.workspace):
                raise ValueError("engine_authority_overlap")
        source = str(spec.workspace)
        if any(character in source for character in (",", "\n", "\r")):
            raise ValueError("unsupported_container_workspace")
        arguments = [
            "create",
            "--pull=never",
            "--name=fleet-" + lease_id,
            "--hostname=" + TOOL_ENVIRONMENT["HOSTNAME"],
            "--label=hi.fleet.lease=" + lease_id,
            "--interactive",
            "--user=1000:1000",
            "--userns=keep-id:uid=1000,gid=1000",
            "--read-only",
            "--read-only-tmpfs=false",
            "--network=none",
            "--pid=private",
            "--ipc=private",
            "--cgroupns=private",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--restart=no",
            "--cpus=" + str(spec.cpus),
            "--memory=" + str(spec.memory_bytes),
            "--memory-swap=" + str(spec.memory_bytes),
            "--pids-limit=" + str(spec.pids_limit),
            "--http-proxy=false",
            "--unsetenv-all",
            "--mount",
            f"type=bind,src={source},dst=/workspace,rw,bind-propagation=rprivate,relabel=private",
            "--tmpfs=/tmp:rw,nosuid,nodev,noexec,size=64m,mode=1777",
            "--workdir=/workspace",
            "--entrypoint=/opt/codex-bin/codex",
        ]
        for name, value in TOOL_ENVIRONMENT.items():
            arguments.extend(["--env", f"{name}={value}"])
        result = self._run(*arguments, spec.image_digest, "exec-server", "--listen", "stdio")
        if result.returncode:
            raise RuntimeError("engine_command_failed")
        return _container_id(result.stdout.decode().strip())

    def inspect(self, container_id: str) -> dict[str, Any]:
        """Return actual engine state for one full container identity."""
        result = self._run("container", "inspect", _container_id(container_id))
        if result.returncode:
            raise RuntimeError("engine_command_failed")
        value = json.loads(result.stdout)
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise ValueError("invalid_engine_inspection")
        return value[0]

    def attach(self, container_id: str) -> subprocess.Popen[bytes]:
        """Attach to one retained endpoint without an implicit restart or shell."""
        process = subprocess.Popen(
            self._argv(
                "start",
                "--attach",
                "--interactive",
                "--sig-proxy=false",
                _container_id(container_id),
            ),
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.attachments.append(process)
        return process

    def remove(self, container_id: str) -> None:
        """Request disposal of one exact container; do not infer process absence."""
        if self._run("rm", "--force", "--time=0", _container_id(container_id)).returncode:
            raise RuntimeError("engine_command_failed")

    def exists(self, container_id: str) -> bool:
        """Distinguish an absent container from an unavailable engine."""
        result = self._run("container", "exists", _container_id(container_id))
        if result.returncode not in (0, 1):
            raise RuntimeError("engine_command_failed")
        return result.returncode == 0

    def close(self) -> None:
        """Close local attachment processes without declaring contained work stopped."""
        for process in self.attachments:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.attachments.clear()


def _read(path: Path) -> str:
    with path.open() as stream:
        value = stream.read(128 * 1024 + 1)
    if len(value) > 128 * 1024:
        raise ValueError("kernel_boundary_unconfirmed")
    return value.strip()


def _start_time(pid: int) -> str:
    value = _read(_PROC / str(pid) / "stat")
    return value[value.rindex(")") + 2 :].split()[19]


class LinuxKernel:
    """Observe the same Linux host as the explicit engine, without a report override."""

    def _scope(self, container_id: str, value: str) -> Path:
        if not isinstance(value, str) or not value.startswith("/") or ".." in Path(value).parts:
            raise ValueError("kernel_boundary_unconfirmed")
        path = _CGROUP / value.lstrip("/")
        if path.name != f"libpod-{_container_id(container_id)}.scope":
            raise ValueError("kernel_boundary_unconfirmed")
        if path.resolve() != path.absolute():
            raise ValueError("kernel_boundary_unconfirmed")
        return path

    def capture(
        self, container_id: str, snapshot: dict[str, Any], spec: ContainerSpec
    ) -> dict[str, Any]:
        """Capture process identities, namespace separation, and cgroup budgets."""
        if sys.platform != "linux":
            raise ValueError("same_host_linux_observation_required")
        try:
            return self._capture(container_id, snapshot, spec)
        except (KeyError, IndexError, TypeError, OSError, ValueError) as error:
            raise ValueError("kernel_boundary_unconfirmed") from error

    def _capture(
        self, container_id: str, snapshot: dict[str, Any], spec: ContainerSpec
    ) -> dict[str, Any]:
        scope = self._scope(container_id, snapshot["State"]["CgroupPath"])
        pid = snapshot["State"]["Pid"]
        if type(pid) is not int or pid <= 0:
            raise ValueError("kernel_boundary_unconfirmed")
        limit_cpu = _read(scope / "cpu.max").split()
        if (
            _read(scope / "memory.max") != str(spec.memory_bytes)
            or _read(scope / "pids.max") != str(spec.pids_limit)
            or len(limit_cpu) != 2
            or int(limit_cpu[1]) <= 0
            or int(limit_cpu[0]) != spec.cpus * int(limit_cpu[1])
        ):
            raise ValueError("kernel_boundary_unconfirmed")
        paths = [scope, *scope.rglob("*")]
        if len(paths) > 4096:
            raise ValueError("kernel_boundary_unconfirmed")
        groups = [path for path in paths if path.is_dir()]
        if len(groups) > 64:
            raise ValueError("kernel_boundary_unconfirmed")
        pids = {int(value) for path in groups for value in _read(path / "cgroup.procs").split()}
        if pid not in pids or len(pids) > 4096:
            raise ValueError("kernel_boundary_unconfirmed")
        processes = []
        for process_id in sorted(pids):
            process = _PROC / str(process_id)
            group = _read(process / "cgroup").removeprefix("0::")
            if not (_CGROUP / group.lstrip("/")).is_relative_to(scope):
                raise ValueError("kernel_boundary_unconfirmed")
            status = dict(
                line.split(":", 1) for line in _read(process / "status").splitlines() if ":" in line
            )
            if status.get("NoNewPrivs", "").strip() != "1" or int(status["CapEff"], 16) != 0:
                raise ValueError("kernel_boundary_unconfirmed")
            for name in ("pid", "mnt", "net", "ipc"):
                if os.readlink(process / "ns" / name) == os.readlink(_PROC / "self/ns" / name):
                    raise ValueError("kernel_boundary_unconfirmed")
            processes.append({"pid": process_id, "startTimeTicks": _start_time(process_id)})
        return {
            "bootId": _read(_PROC / "sys/kernel/random/boot_id"),
            "containerId": container_id,
            "cgroupPath": str(scope.relative_to(_CGROUP)),
            "cgroupInode": scope.stat().st_ino,
            "processes": processes,
            "limits": {
                "memoryBytes": spec.memory_bytes,
                "cpus": spec.cpus,
                "pidsLimit": spec.pids_limit,
            },
        }

    def absent(self, before: dict[str, Any]) -> bool:
        """Require the original boot, cgroup absence, and absence of every original process."""
        if sys.platform != "linux" or before["bootId"] != _read(
            _PROC / "sys/kernel/random/boot_id"
        ):
            return False
        scope = self._scope(before["containerId"], "/" + before["cgroupPath"].lstrip("/"))
        try:
            scope.stat()
        except FileNotFoundError:
            pass
        else:
            return False
        for process in before["processes"]:
            try:
                if _start_time(process["pid"]) == process["startTimeTicks"]:
                    return False
            except FileNotFoundError:
                continue
        return True
