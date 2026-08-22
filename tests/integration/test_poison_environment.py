"""Integration coverage proving retired environment names cannot configure CLIs."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents.runtime import _pi_env
from hephaestus.automation import loop_runner
from hephaestus.forensics import coredump_handler, gdb_runner
from hephaestus.github.fleet_sync import cli as fleet_cli
from hephaestus.nats.config import NATSConfig


@pytest.fixture
def poisoned_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set representative retired names to valid-looking but hostile values."""
    values = {
        "HEPH_PLANNER_MODEL": "poison-model",
        "HEPH_AGENT_DEFAULT_TIMEOUT": "999999",
        "HEPHAESTUS_LOG_FORMAT": "json",
        "HEPHAESTUS_RATE_GUARD_THRESHOLD": "999999",
        "NATS_URL": "nats://poison.invalid:4222",
        "NATS_TLS": "false",
        "PI_CODING_AGENT_DIR": "/poison/pi",
        "HEPH_PI_ISOLATION_ADAPTER": "poison-adapter",
        "FLEET_ORG": "poison-org",
        "FLEET_REPOS": "poison-repo",
        "COREDUMP_MAX_BYTES": "1",
        "COREDUMP_TARGET_DIRS": "/poison/core",
        "RUN_UNDER_GDB": "1",
        "GDB_CMD_PREFIX": "poison-wrapper",
        "PROJECTS_ROOT": "/poison/projects",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_poisoned_names_do_not_change_cli_or_config_defaults(
    poisoned_environment: None,
) -> None:
    """Representative CLI boundaries retain typed defaults under poison input."""
    loop = loop_runner._build_parser().parse_args([])
    coredump = coredump_handler._build_parser().parse_args([])
    gdb = gdb_runner._build_parser().parse_args(["/tmp/core", "true"])
    fleet = fleet_cli._build_parser().parse_args([])
    nats = NATSConfig()

    assert loop.planner_model == ""
    assert loop.log_format == "text"
    assert loop.rate_guard_threshold == 200
    assert loop.pi_isolation_adapter is None
    assert loop.pi_dir is None
    assert coredump.target_dir is None
    assert coredump.max_bytes == coredump_handler.DEFAULT_MAX_BYTES
    assert gdb.direct is False
    assert gdb.gdb_cmd_prefix is None
    assert fleet.org is None and fleet.repos is None
    assert nats.url == "tls://localhost:4222" and nats.tls is True


def test_poisoned_pi_directory_is_not_forwarded_without_explicit_value(
    poisoned_environment: None,
) -> None:
    """The Pi child boundary ignores an ambient package-directory setting."""
    environment = _pi_env(temp_dir=Path("/tmp/hephaestus-pi-test"))

    assert "PI_CODING_AGENT_DIR" not in environment
    assert environment["TMPDIR"] == "/tmp/hephaestus-pi-test"
