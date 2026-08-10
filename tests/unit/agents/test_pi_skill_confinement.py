"""Tests for Pi Athena skill capability confinement."""
# ruff: noqa: D103

from __future__ import annotations

from pathlib import Path

from hephaestus.agents.pi_plugins import (
    InventoryResult,
    PiAthenaCommandReceipt,
    PiPreflightResult,
    load_pi_package_catalog,
    prove_athena_skill_command,
)


def _ready_preflight(tmp_path: Path) -> PiPreflightResult:
    inventory = InventoryResult(
        ready=True,
        status="ready",
        roots={
            "athena": tmp_path / "athena",
            "pi-subagents": tmp_path / "subagents",
            "pi-web-access": tmp_path / "web",
        },
        scopes={"athena": "user", "pi-subagents": "user", "pi-web-access": "user"},
    )
    return PiPreflightResult.ready_result(inventory)


def test_preflight_inventory_records_pinned_athena_commands(tmp_path: Path) -> None:
    preflight = _ready_preflight(tmp_path)
    catalog = load_pi_package_catalog()

    advise = prove_athena_skill_command("skill:advise", preflight)
    learn = prove_athena_skill_command("skill:learn", preflight)

    assert advise == PiAthenaCommandReceipt(
        command="skill:advise",
        package_key="athena",
        package_root=str(tmp_path / "athena"),
        repository=catalog.packages[0].identity,
        commit=catalog.packages[0].pin,
    )
    assert learn.command == "skill:learn"
    assert learn.commit == catalog.packages[0].pin
