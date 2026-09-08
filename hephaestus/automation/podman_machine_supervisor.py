"""Prepare one Podman machine for a host-owned automation-loop run.

The automation-loop process owns this preflight. The local CI runner does not
start, stop, remove, or recreate Podman machines.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.config.child_environments import read_approved_parent_env

LOG = logging.getLogger(__name__)

CommandRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
_MACHINE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_EMPTY_LAST_UP = {"", "0001-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00"}


class PodmanMachineError(RuntimeError):
    """Report a fail-closed Podman machine preparation error."""


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
    root = data_home if data_home is not None else Path.home() / ".local" / "share"
    return root / "containers" / "podman" / "machine" / "machine-start.lock"


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
            lines = serial_path.read_text(encoding="utf-8", errors="replace").splitlines()
            detail = "\n".join(lines[-200:])
        except OSError as exc:
            detail = f"could not read serial log: {exc}"
        evidence.append(f"serial log ({serial_path}, last 200 lines):\n{detail or 'empty'}")
    return "\n\n".join(evidence)


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
    if not _MACHINE_NAME.fullmatch(name):
        raise PodmanMachineError(f"Invalid Podman machine name: {name!r}.")
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
    provider = _provider(document)
    if provider != "applehv":
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
