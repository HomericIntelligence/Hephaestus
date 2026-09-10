"""Prepare one Podman machine for a host-owned automation-loop run.

The automation-loop process owns this preflight. The local CI runner does not
start, stop, remove, or recreate Podman machines.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from hephaestus.config.child_environments import read_approved_parent_env

LOG = logging.getLogger(__name__)

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
_MACHINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SERIAL_TAIL_BYTES = 16 * 1024
_EMPTY_LAST_UP = {"", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00"}


class PodmanMachineError(RuntimeError):
    """Report a fail-closed Podman machine preparation error."""


def validate_podman_machine_name(name: str) -> None:
    """Reject names that cannot identify one Podman machine."""
    if not isinstance(name, str) or not _MACHINE_NAME.fullmatch(name):
        raise PodmanMachineError("Invalid Podman machine name.")


def _run_command(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run one noninteractive host command with a finite timeout."""
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=read_approved_parent_env(),
    )


def _inspect_machine(
    name: str,
    *,
    timeout_s: float,
    command_runner: CommandRunner,
) -> dict[str, Any]:
    """Return the selected machine inspection document."""
    command = ["podman", "machine", "inspect", name]
    try:
        result = command_runner(command, timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PodmanMachineError(f"Podman could not inspect machine {name!r}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"Podman could not inspect machine {name!r}."
        raise PodmanMachineError(detail)
    try:
        documents = json.loads(result.stdout)
        document = documents[0] if isinstance(documents, list) else documents
    except (IndexError, json.JSONDecodeError, TypeError) as exc:
        raise PodmanMachineError(
            f"Podman returned invalid inspection data for machine {name!r}."
        ) from exc
    if not isinstance(document, dict):
        raise PodmanMachineError(f"Podman returned invalid inspection data for machine {name!r}.")
    return document


def _provider(document: dict[str, Any]) -> str:
    """Return the provider name from supported Podman inspection shapes."""
    vm_type = document.get("VMType")
    if isinstance(vm_type, str) and vm_type:
        return vm_type.lower()
    config_dir = document.get("ConfigDir")
    if isinstance(config_dir, dict):
        path = config_dir.get("Path")
        if isinstance(path, str) and path:
            return Path(path).name.lower()
    return ""


def _named_connection_matches_machine(document: dict[str, Any], connection: dict[str, Any]) -> bool:
    """Return whether one named connection identifies the inspected machine."""
    if connection.get("IsMachine") is not True:
        return False
    uri = connection.get("URI")
    if not isinstance(uri, str):
        return False
    try:
        endpoint = urlsplit(uri)
        port = endpoint.port
    except ValueError:
        return False
    machine_connection = document.get("ConnectionInfo")
    socket = (
        machine_connection.get("PodmanSocket") if isinstance(machine_connection, dict) else None
    )
    socket_path = socket.get("Path") if isinstance(socket, dict) else None
    if endpoint.scheme == "unix":
        return isinstance(socket_path, str) and unquote(endpoint.path) == socket_path
    if endpoint.scheme != "ssh":
        return False
    ssh = document.get("SSHConfig")
    if not isinstance(ssh, dict):
        return False
    identity = connection.get("Identity")
    return (
        endpoint.hostname in {"127.0.0.1", "localhost"}
        and endpoint.username == ssh.get("RemoteUsername")
        and port == ssh.get("Port")
        and isinstance(identity, str)
        and identity == ssh.get("IdentityPath")
    )


def _verify_named_connection(
    name: str,
    document: dict[str, Any],
    *,
    timeout_s: float,
    command_runner: CommandRunner,
) -> None:
    """Verify that the selected name maps to the inspected machine endpoint."""
    command = ["podman", "system", "connection", "list", "--format", "json"]
    try:
        result = command_runner(command, timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PodmanMachineError(
            f"Podman could not inspect named connection {name!r}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "Podman could not inspect named connections."
        raise PodmanMachineError(detail)
    try:
        connections = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise PodmanMachineError("Podman returned invalid named-connection data.") from exc
    selected: list[dict[str, Any]] = []
    if isinstance(connections, list):
        selected = [
            connection
            for connection in connections
            if isinstance(connection, dict) and connection.get("Name") == name
        ]
    if len(selected) != 1 or not _named_connection_matches_machine(document, selected[0]):
        raise PodmanMachineError(
            f"Podman named connection {name!r} does not match the selected machine endpoint."
        )


def _serial_log_path(document: dict[str, Any], name: str) -> Path | None:
    """Derive the machine serial-log path from its socket directory."""
    connection = document.get("ConnectionInfo")
    if not isinstance(connection, dict):
        return Path(tempfile.gettempdir()) / "podman" / f"{name}.log"
    socket = connection.get("PodmanSocket")
    if not isinstance(socket, dict):
        return Path(tempfile.gettempdir()) / "podman" / f"{name}.log"
    socket_path = socket.get("Path")
    if not isinstance(socket_path, str) or not socket_path:
        return Path(tempfile.gettempdir()) / "podman" / f"{name}.log"
    return Path(socket_path).parent / f"{name}.log"


def _start_lock_path(data_home: Path | None = None) -> Path:
    """Return Podman's global machine-start lock path."""
    if data_home is None:
        environment = read_approved_parent_env()
        home = Path(environment["HOME"]) if environment.get("HOME") else Path.home()
        data_home = Path(environment.get("XDG_DATA_HOME") or home / ".local" / "share")
    return data_home / "containers" / "podman" / "machine" / "machine-start.lock"


def _read_serial_tail(path: Path) -> str:
    """Read at most 16 KiB and retain at most 200 recent log lines."""
    with path.open("rb") as stream:
        size = stream.seek(0, os.SEEK_END)
        stream.seek(max(0, size - _SERIAL_TAIL_BYTES))
        raw = stream.read(_SERIAL_TAIL_BYTES)
    text = "\n".join(raw.decode("utf-8", errors="replace").splitlines()[-200:])
    # Replacement characters can expand invalid bytes. Bound the encoded result too.
    return text.encode("utf-8")[-_SERIAL_TAIL_BYTES:].decode("utf-8", errors="ignore")


def _failure_evidence(
    document: dict[str, Any],
    name: str,
    *,
    command_runner: CommandRunner,
    data_home: Path | None = None,
) -> str:
    """Collect bounded, read-only lock and serial-log evidence."""
    evidence: list[str] = []
    lock_path = _start_lock_path(data_home)
    if lock_path.exists():
        try:
            result = command_runner(["lsof", str(lock_path)], 5)
            detail = (result.stdout or result.stderr).strip()
        except (OSError, subprocess.SubprocessError) as exc:
            detail = f"could not inspect lock owner: {exc}"
        evidence.append(f"machine-start lock ({lock_path}):\n{detail or 'no owner reported'}")

    serial_path = _serial_log_path(document, name)
    if serial_path is not None:
        try:
            detail = _read_serial_tail(serial_path)
        except OSError as exc:
            detail = f"could not read serial log: {exc}"
        evidence.append(
            f"serial log ({serial_path}, last 200 lines, at most 16 KiB):\n{detail or 'empty'}"
        )
    return "\n\n".join(evidence)


def _require_applehv(
    document: dict[str, Any],
    name: str,
    *,
    command_runner: CommandRunner,
    data_home: Path | None,
) -> None:
    """Reject an inspection document that does not select AppleHV."""
    provider = _provider(document)
    if provider == "applehv":
        return
    evidence = _failure_evidence(
        document,
        name,
        command_runner=command_runner,
        data_home=data_home,
    )
    raise PodmanMachineError(
        f"Podman machine {name!r} must use AppleHV; inspection reported "
        f"{provider or 'unknown'}.\n{evidence}".rstrip()
    )


def prepare_podman_machine(
    name: str,
    *,
    start_timeout_s: float = 120,
    health_timeout_s: float = 60,
    command_runner: CommandRunner = _run_command,
    data_home: Path | None = None,
) -> None:
    """Start and verify one AppleHV machine for the automation-loop lifetime.

    The function never stops, removes, or recreates a machine. It raises
    :class:`PodmanMachineError` before pipeline dispatch when readiness cannot
    be proved.
    """
    validate_podman_machine_name(name)
    if start_timeout_s <= 0 or health_timeout_s <= 0:
        raise PodmanMachineError("Podman machine timeouts must be positive.")

    try:
        document = _inspect_machine(
            name,
            timeout_s=min(start_timeout_s, 30),
            command_runner=command_runner,
        )
    except PodmanMachineError as exc:
        evidence = _failure_evidence(
            {},
            name,
            command_runner=command_runner,
            data_home=data_home,
        )
        raise PodmanMachineError(f"{exc}\n{evidence}".rstrip()) from exc
    _require_applehv(
        document,
        name,
        command_runner=command_runner,
        data_home=data_home,
    )

    state = str(document.get("State", "")).lower()
    if state != "running":
        command = ["podman", "machine", "start", name]
        try:
            result = command_runner(command, start_timeout_s)
        except (OSError, subprocess.SubprocessError) as exc:
            evidence = _failure_evidence(
                document,
                name,
                command_runner=command_runner,
                data_home=data_home,
            )
            raise PodmanMachineError(
                f"Podman could not start machine {name!r}: {exc}\n{evidence}".rstrip()
            ) from exc
        if result.returncode != 0:
            evidence = _failure_evidence(
                document,
                name,
                command_runner=command_runner,
                data_home=data_home,
            )
            detail = (result.stderr or result.stdout).strip()
            raise PodmanMachineError(
                f"Podman could not start machine {name!r}: {detail}\n{evidence}".rstrip()
            )
        try:
            document = _inspect_machine(
                name,
                timeout_s=min(health_timeout_s, 30),
                command_runner=command_runner,
            )
        except PodmanMachineError as exc:
            evidence = _failure_evidence(
                document,
                name,
                command_runner=command_runner,
                data_home=data_home,
            )
            raise PodmanMachineError(f"{exc}\n{evidence}".rstrip()) from exc
        _require_applehv(
            document,
            name,
            command_runner=command_runner,
            data_home=data_home,
        )

    state = str(document.get("State", "")).lower()
    last_up = str(document.get("LastUp", "")).strip()
    if state != "running" or last_up in _EMPTY_LAST_UP:
        evidence = _failure_evidence(
            document,
            name,
            command_runner=command_runner,
            data_home=data_home,
        )
        raise PodmanMachineError(
            f"Podman machine {name!r} is not ready: State={state or 'unknown'}, "
            f"LastUp={last_up or 'empty'}.\n{evidence}".rstrip()
        )

    try:
        _verify_named_connection(
            name,
            document,
            timeout_s=min(health_timeout_s, 30),
            command_runner=command_runner,
        )
    except PodmanMachineError as exc:
        evidence = _failure_evidence(
            document,
            name,
            command_runner=command_runner,
            data_home=data_home,
        )
        raise PodmanMachineError(f"{exc}\n{evidence}".rstrip()) from exc

    health_command = ["podman", "--connection", name, "info"]
    try:
        health = command_runner(health_command, health_timeout_s)
    except (OSError, subprocess.SubprocessError) as exc:
        evidence = _failure_evidence(
            document,
            name,
            command_runner=command_runner,
            data_home=data_home,
        )
        raise PodmanMachineError(
            f"Podman health check failed for machine {name!r}: {exc}\n{evidence}".rstrip()
        ) from exc
    if health.returncode != 0:
        evidence = _failure_evidence(
            document,
            name,
            command_runner=command_runner,
            data_home=data_home,
        )
        detail = (health.stderr or health.stdout).strip()
        raise PodmanMachineError(
            f"Podman health check failed for machine {name!r}: {detail}\n{evidence}".rstrip()
        )
    LOG.info("Podman machine %s is ready (provider=applehv, LastUp=%s).", name, last_up)
