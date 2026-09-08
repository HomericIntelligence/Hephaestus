"""Behavior tests for the host-owned Podman machine supervisor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation.podman_machine_supervisor import (
    PodmanMachineError,
    prepare_podman_machine,
)


def _inspect(
    *, state: str, provider: str = "applehv", last_up: str = "2026-09-07T01:02:03Z"
) -> str:
    """Return one representative Podman machine inspection document."""
    return json.dumps(
        [
            {
                "ConfigDir": {"Path": f"/tmp/config/machine/{provider}"},
                "ConnectionInfo": {"PodmanSocket": {"Path": "/tmp/podman/hephaestus-ci.sock"}},
                "LastUp": last_up,
                "Name": "hephaestus-ci",
                "State": state,
            }
        ]
    )


class CommandHarness:
    """Return configured command results and record all calls."""

    def __init__(
        self,
        responses: dict[tuple[str, ...], list[subprocess.CompletedProcess[str]]],
    ) -> None:
        self.responses = responses
        self.calls: list[tuple[tuple[str, ...], float]] = []

    def __call__(self, command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        self.calls.append((key, timeout))
        response = self.responses[key].pop(0)
        return response


def _result(
    command: tuple[str, ...], *, stdout: str = "", returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def test_starts_stopped_applehv_machine_and_probes_its_named_connection() -> None:
    """The host starts only the selected machine before it reports ready."""
    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    start_command = ("podman", "machine", "start", "hephaestus-ci")
    health_command = ("podman", "--connection", "hephaestus-ci", "info")
    runner = CommandHarness(
        {
            inspect_command: [
                _result(inspect_command, stdout=_inspect(state="stopped", last_up="")),
                _result(inspect_command, stdout=_inspect(state="running")),
            ],
            start_command: [_result(start_command)],
            health_command: [_result(health_command, stdout="{}")],
        }
    )

    prepare_podman_machine(
        "hephaestus-ci",
        start_timeout_s=30,
        health_timeout_s=10,
        command_runner=runner,
    )

    commands = [command for command, _timeout in runner.calls]
    assert commands == [inspect_command, start_command, inspect_command, health_command]
    assert all("rm" not in command and "stop" not in command for command in commands)


def test_rejects_non_applehv_machine_without_starting_it(tmp_path: Path) -> None:
    """The macOS supervisor fails closed when the selected provider is not AppleHV."""
    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    runner = CommandHarness(
        {
            inspect_command: [
                _result(inspect_command, stdout=_inspect(state="stopped", provider="qemu"))
            ]
        }
    )

    with pytest.raises(PodmanMachineError, match="AppleHV"):
        prepare_podman_machine(
            "hephaestus-ci",
            command_runner=runner,
            data_home=tmp_path,
        )

    assert [command for command, _timeout in runner.calls] == [inspect_command]


@pytest.mark.parametrize("name", ["", "../other", "name with spaces"])
def test_rejects_invalid_machine_name_before_any_command(name: str) -> None:
    """An invalid name cannot change command scope."""
    runner = CommandHarness({})

    with pytest.raises(PodmanMachineError, match="Invalid"):
        prepare_podman_machine(name, command_runner=runner)

    assert runner.calls == []


@pytest.mark.parametrize(
    ("start_timeout", "health_timeout"),
    [(0, 1), (1, 0), (-1, 1), (1, -1)],
)
def test_rejects_unbounded_timeout_values(
    start_timeout: int,
    health_timeout: int,
) -> None:
    """All host subprocess bounds must be positive."""
    runner = CommandHarness({})

    with pytest.raises(PodmanMachineError, match="positive"):
        prepare_podman_machine(
            "hephaestus-ci",
            start_timeout_s=start_timeout,
            health_timeout_s=health_timeout,
            command_runner=runner,
        )

    assert runner.calls == []


@pytest.mark.parametrize("stdout", ["not-json", "[]", "null"])
def test_rejects_invalid_inspection_documents(stdout: str, tmp_path: Path) -> None:
    """Malformed Podman output cannot authorize startup."""
    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    runner = CommandHarness({inspect_command: [_result(inspect_command, stdout=stdout)]})

    with pytest.raises(PodmanMachineError, match="invalid inspection"):
        prepare_podman_machine(
            "hephaestus-ci",
            command_runner=runner,
            data_home=tmp_path,
        )


def test_start_failure_captures_serial_log_and_stops(tmp_path: Path) -> None:
    """A failed start returns evidence without a second machine mutation."""
    serial_dir = tmp_path / "podman"
    serial_dir.mkdir()
    (serial_dir / "hephaestus-ci.log").write_text("start failed\n", encoding="utf-8")
    inspect = json.loads(_inspect(state="stopped", last_up=""))
    inspect[0]["ConnectionInfo"]["PodmanSocket"]["Path"] = str(serial_dir / "hephaestus-ci.sock")
    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    start_command = ("podman", "machine", "start", "hephaestus-ci")
    runner = CommandHarness(
        {
            inspect_command: [_result(inspect_command, stdout=json.dumps(inspect))],
            start_command: [_result(start_command, returncode=125)],
        }
    )

    with pytest.raises(PodmanMachineError, match="start failed"):
        prepare_podman_machine(
            "hephaestus-ci",
            command_runner=runner,
            data_home=tmp_path / "data",
        )

    assert [command for command, _timeout in runner.calls] == [inspect_command, start_command]


def test_rejects_running_machine_without_last_up(tmp_path: Path) -> None:
    """A running state without LastUp is not sufficient readiness evidence."""
    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    runner = CommandHarness(
        {inspect_command: [_result(inspect_command, stdout=_inspect(state="running", last_up=""))]}
    )

    with pytest.raises(PodmanMachineError, match="LastUp"):
        prepare_podman_machine(
            "hephaestus-ci",
            health_timeout_s=1,
            command_runner=runner,
            data_home=tmp_path,
        )


def test_failure_collects_lock_owner_and_serial_log_without_recovery_mutation(
    tmp_path: Path,
) -> None:
    """A failed health probe records evidence but does not stop or remove a machine."""
    data_home = tmp_path / "data"
    lock_path = data_home / "containers" / "podman" / "machine" / "machine-start.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.touch()
    serial_dir = tmp_path / "podman"
    serial_dir.mkdir()
    (serial_dir / "hephaestus-ci.log").write_text("boot line\nfatal line\n", encoding="utf-8")

    inspect_command = ("podman", "machine", "inspect", "hephaestus-ci")
    health_command = ("podman", "--connection", "hephaestus-ci", "info")
    lsof_command = ("lsof", str(lock_path))
    inspect = json.loads(_inspect(state="running"))
    inspect[0]["ConnectionInfo"]["PodmanSocket"]["Path"] = str(serial_dir / "hephaestus-ci.sock")
    runner = CommandHarness(
        {
            inspect_command: [_result(inspect_command, stdout=json.dumps(inspect))],
            health_command: [_result(health_command, returncode=125)],
            lsof_command: [_result(lsof_command, stdout="COMMAND PID USER\npodman 42 user")],
        }
    )

    with pytest.raises(PodmanMachineError) as excinfo:
        prepare_podman_machine(
            "hephaestus-ci",
            health_timeout_s=1,
            command_runner=runner,
            data_home=data_home,
        )

    detail = str(excinfo.value)
    assert "podman 42 user" in detail
    assert "fatal line" in detail
    commands = [command for command, _timeout in runner.calls]
    assert commands == [inspect_command, health_command, lsof_command]
    assert all(not ({"rm", "stop", "reset"} & set(command)) for command in commands)


def test_main_does_not_dispatch_when_podman_preflight_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed host preflight prevents all pipeline work."""
    from hephaestus.automation import pipeline_cli
    from hephaestus.automation.pipeline import coordinator as coordinator_mod

    dispatched = False

    def fail_preflight(*_args: Any, **_kwargs: Any) -> None:
        raise PodmanMachineError("not ready")

    def record_dispatch(*_args: Any, **_kwargs: Any) -> int:
        nonlocal dispatched
        dispatched = True
        return 0

    monkeypatch.setattr(pipeline_cli, "prepare_podman_machine", fail_preflight)
    monkeypatch.setattr(coordinator_mod, "run_pipeline", record_dispatch)

    result = pipeline_cli.main(["--podman-machine", "hephaestus-ci", "--agent", "codex"])

    assert result == 1
    assert dispatched is False


def test_full_queue_parser_accepts_bounded_podman_machine_options() -> None:
    """The full queue accepts one selected machine and bounded timeouts."""
    from hephaestus.automation import pipeline_cli

    args = pipeline_cli.parse_args(
        [
            "--podman-machine",
            "hephaestus-ci",
            "--podman-start-timeout",
            "30",
            "--podman-health-timeout",
            "10",
        ]
    )

    assert args.podman_machine == "hephaestus-ci"
    assert args.podman_start_timeout == 30
    assert args.podman_health_timeout == 10
