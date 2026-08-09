"""Tests for Pi Athena skill capability confinement."""
# ruff: noqa: D103
# ruff: noqa: D103

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.agents.pi_plugins import (
    InventoryResult,
    PiAthenaCommandReceipt,
    PiPreflightResult,
    load_pi_package_catalog,
    prove_athena_skill_command,
)
from hephaestus.agents.runtime import PI_READ_ONLY_TOOLS, pi_athena_invocation_args


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


def test_preflight_proves_athena_advise_and_learn_commands(tmp_path: Path) -> None:
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


@pytest.mark.parametrize(
    ("kind", "command"), [("advise", "skill:advise"), ("learn", "skill:learn")]
)
def test_pi_athena_args_grant_only_base_tools_and_one_skill_command(
    tmp_path: Path,
    kind: str,
    command: str,
) -> None:
    args = pi_athena_invocation_args(kind, _ready_preflight(tmp_path))

    assert args == ("--tools", PI_READ_ONLY_TOOLS, "--commands", command)
    joined = ",".join(args)
    assert "Mnemosyne" not in joined
    assert "bash" not in joined
    assert "gh" not in joined


def test_missing_preflight_receipt_fails_closed() -> None:
    preflight = PiPreflightResult(True, "ready", "")

    with pytest.raises(ValueError, match="not proven"):
        pi_athena_invocation_args("advise", preflight)


def test_unsupported_athena_kind_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unsupported Pi Athena skill kind"):
        pi_athena_invocation_args("pr-review", _ready_preflight(tmp_path))
