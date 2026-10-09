"""Behavior tests for the Pi package bootstrap and preflight contract."""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import skipUnless
from unittest.mock import Mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_console_script_is_registered() -> None:
    """Operators receive the single documented Pi bootstrap entry point."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["hephaestus-install-pi-plugins"] == (
        "hephaestus.agents.pi_plugins:main"
    )


def test_packaged_catalog_is_the_exact_pin_authority() -> None:
    """Every Pi package and the CLI itself are represented by immutable pins."""
    catalog_path = REPO_ROOT / "hephaestus" / "agents" / "pi_package_catalog.json"
    assert catalog_path.is_file(), "the distributable Pi package catalog is missing"

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["compatibility"]["pi"] == {
        "npm_name": "@earendil-works/pi-coding-agent",
        "version": "0.80.2",
    }
    assert catalog["packages"]["athena"]["commit"] == ("44a22b8dfab986f505a99ce52e8521f645da3e2b")
    assert catalog["packages"]["athena"]["name"] == "@homericintelligence/athena"
    assert catalog["packages"]["athena"]["manifest_version"] == "0.5.0"
    assert catalog["packages"]["pi-subagents"]["version"] == "0.37.2"
    assert catalog["packages"]["pi-web-access"]["version"] == "0.15.0"


def test_catalog_builds_only_immutable_native_install_specs() -> None:
    """The installer derives argv from validated catalog pins, not literals."""
    from hephaestus.agents.pi_plugins import load_pi_package_catalog

    catalog = load_pi_package_catalog()

    assert catalog.packages[0].manifest_version == "0.5.0"
    assert catalog.install_specs == (
        "git:github.com/HomericIntelligence/Athena@44a22b8dfab986f505a99ce52e8521f645da3e2b",
        "npm:pi-subagents@0.37.2",
        "npm:pi-web-access@0.15.0",
    )


def _fake_pi_install(tmp_path: Path, *, version: str = "0.80.2") -> Path:
    package_root = tmp_path / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent"
    executable = package_root / "dist" / "pi"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (package_root / "package.json").write_text(
        json.dumps({"name": "@earendil-works/pi-coding-agent", "version": version}),
        encoding="utf-8",
    )
    return executable


def test_cli_identity_and_version_match_catalog(tmp_path: Path) -> None:
    """The exact executable is bound to its npm manifest and version output."""
    from hephaestus.agents.pi_plugins import (
        ProcessResult,
        load_pi_package_catalog,
        probe_pi_cli_identity,
    )

    executable = _fake_pi_install(tmp_path)

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessResult:
        assert argv == (str(executable.resolve()), "--version")
        return ProcessResult(returncode=0, stdout="pi 0.80.2\n", stderr="")

    result = probe_pi_cli_identity(executable, load_pi_package_catalog(), runner=runner)

    assert result.ready is True
    assert result.status == "ready"
    assert result.executable == executable.resolve()


def test_cli_version_mismatch_stops_before_install_or_extension(tmp_path: Path) -> None:
    """An incompatible CLI is rejected before any package code can execute."""
    from hephaestus.agents.pi_plugins import (
        ProcessResult,
        load_pi_package_catalog,
        probe_pi_cli_identity,
    )

    executable = _fake_pi_install(tmp_path)
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> ProcessResult:
        calls.append(argv)
        return ProcessResult(returncode=0, stdout="0.80.1\n", stderr="")

    result = probe_pi_cli_identity(executable, load_pi_package_catalog(), runner=runner)

    assert result.ready is False
    assert result.status == "pi_cli_version_mismatch"
    assert calls == [(str(executable.resolve()), "--version")]
    assert "npm install -g --ignore-scripts" in result.remediation


def test_dry_run_emits_exact_argv_without_subprocess_or_filesystem_writes() -> None:
    """Dry-run is a pure preview of every planned subprocess."""
    from hephaestus.agents.pi_plugins import (
        InstallOptions,
        install_pi_plugins,
        load_pi_package_catalog,
    )

    def forbidden_runner(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("dry-run must not execute a subprocess")

    report = install_pi_plugins(
        InstallOptions(dry_run=True, json_output=True, project_local=True, approve=True),
        catalog=load_pi_package_catalog(),
        pi_bin=Path("/opt/pi/bin/pi"),
        runner=forbidden_runner,
    )

    assert report.ready is False
    assert report.status == "dry_run"
    assert report.commands[0] == ("/opt/pi/bin/pi", "--version")
    assert report.commands[1] == (
        "/opt/pi/bin/pi",
        "install",
        "git:github.com/HomericIntelligence/Athena@44a22b8dfab986f505a99ce52e8521f645da3e2b",
        "-l",
        "--approve",
    )


def test_successful_installs_run_post_install_preflight(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Installation is not reported ready until the same package gate passes."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    executable = _fake_pi_install(tmp_path)
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
    pi_dir = tmp_path / "pi-agent"

    def runner(argv: tuple[str, ...], **kwargs: Any) -> pi_plugins.ProcessResult:
        calls.append((argv, kwargs.get("env")))
        if argv[-1] == "--version":
            return pi_plugins.ProcessResult(0, "0.80.2\n", "")
        return pi_plugins.ProcessResult(0, "installed\n", "")

    ready = pi_plugins.PiPreflightResult.ready_result()
    preflight = Mock(return_value=ready)
    monkeypatch.setattr(pi_plugins, "preflight_pi_environment", preflight)

    report = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True, pi_dir=pi_dir),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )

    assert report.ready is True
    assert report.status == "ready"
    assert len(calls) == 4
    assert all(call[1] is not None for call in calls[1:])
    assert all(call[1]["PI_CODING_AGENT_DIR"] == str(pi_dir) for call in calls[1:] if call[1])
    install_environments = [call[1] for call in calls[1:] if call[1] is not None]
    assert install_environments[0]["NPM_CONFIG_PACKAGE_LOCK"] == "false"
    assert all("NPM_CONFIG_PACKAGE_LOCK" not in env for env in install_environments[1:])
    assert preflight.call_args.kwargs["pi_dir"] == pi_dir
    assert preflight.call_args.kwargs["trust_override"] == "--no-approve"


def _write_package(root: Path, name: str, version: str) -> None:
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )


def test_inventory_respects_pi_coding_agent_dir_and_exact_package_identity(
    tmp_path: Path,
) -> None:
    """User-scoped npm packages resolve beneath the admitted Pi settings root."""
    from hephaestus.agents import pi_plugins

    pi_dir = tmp_path / "pi-home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    catalog = pi_plugins.load_pi_package_catalog()
    (pi_dir / "settings.json").parent.mkdir(parents=True)
    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    user_npm_root = pi_dir / "npm" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "0.5.0")
    _write_package(user_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(user_npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = pi_plugins.inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=lambda root: catalog.packages[0].pin if root == athena_root else "",
        git_status=lambda _root: "",
    )

    assert result.ready is True
    assert result.status == "ready"
    assert result.roots["athena"] == athena_root.resolve()
    assert result.roots["pi-subagents"] == (user_npm_root / "pi-subagents").resolve()
    assert set(result.scopes.values()) == {"user"}


def test_project_inventory_uses_pi_npm_node_modules_layout(tmp_path: Path) -> None:
    """Project-local npm packages resolve below ``.pi/npm/node_modules``."""
    from hephaestus.agents.pi_plugins import inspect_pi_package_inventory, load_pi_package_catalog

    cwd = tmp_path / "repo"
    project_root = cwd / ".pi"
    project_root.mkdir(parents=True)
    catalog = load_pi_package_catalog()
    (project_root / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    athena_root = project_root / "git" / "github.com" / "HomericIntelligence" / "Athena"
    project_npm_root = project_root / "npm" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "0.5.0")
    _write_package(project_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(project_npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=tmp_path / "pi-home",
        git_head=lambda root: catalog.packages[0].pin if root == athena_root else "",
        git_status=lambda _root: "",
    )

    assert result.ready is True
    assert result.roots["pi-web-access"] == (project_npm_root / "pi-web-access").resolve()
    assert set(result.scopes.values()) == {"project"}


def test_inventory_rejects_packages_outside_the_pinned_catalog(tmp_path: Path) -> None:
    """Preflight and execution cannot observe different package sets."""
    from hephaestus.agents import pi_plugins

    pi_dir = tmp_path / "pi-home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    catalog = pi_plugins.load_pi_package_catalog()
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": [*catalog.install_specs, "npm:extra-private-package@1.0.0"]}),
        encoding="utf-8",
    )

    result = pi_plugins.inspect_pi_package_inventory(cwd, catalog, pi_dir=pi_dir)

    assert result.ready is False
    assert result.status == "package_inventory_mismatch"


def test_inventory_rejects_dirty_git_package_at_pinned_head(tmp_path: Path) -> None:
    """A pinned commit cannot hide modified or untracked executable content."""
    from hephaestus.agents import pi_plugins

    pi_dir = tmp_path / "pi-home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    catalog = pi_plugins.load_pi_package_catalog()
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    npm_root = pi_dir / "npm" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "0.5.0")
    _write_package(npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = pi_plugins.inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=lambda _root: catalog.packages[0].pin,
        git_status=lambda _root: "?? skills/injected/SKILL.md",
    )

    assert result.ready is False
    assert result.status == "package_content_mismatch"
    assert result.detail == "athena: ?? skills/injected/SKILL.md"


def test_installer_discards_only_its_generated_git_package_lockfile(tmp_path: Path) -> None:
    """Pi's npm bootstrap lock file must not mask other checkout mutations."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    pi_dir = tmp_path / "pi-home"
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    athena_root.mkdir(parents=True)
    package_lock = athena_root / "package-lock.json"
    package_lock.write_text("{}", encoding="utf-8")

    pi_plugins.discard_generated_git_package_lockfiles(
        catalog,
        pi_dir=pi_dir,
        project_local=False,
        git_status=lambda _root: "?? package-lock.json",
    )

    assert not package_lock.exists()

    package_lock.write_text("{}", encoding="utf-8")
    pi_plugins.discard_generated_git_package_lockfiles(
        catalog,
        pi_dir=pi_dir,
        project_local=False,
        git_status=lambda _root: "?? package-lock.json\n?? skills/injected/SKILL.md",
    )

    assert package_lock.exists()


def test_probe_requires_verified_source_info_provenance(tmp_path: Path) -> None:
    """A colliding command or tool name from another root cannot satisfy preflight."""
    from hephaestus.agents.pi_plugins import (
        InventoryResult,
        load_pi_package_catalog,
        verify_capability_inventory,
    )

    catalog = load_pi_package_catalog()
    roots = {package.key: tmp_path / package.key for package in catalog.packages}
    inventory = InventoryResult(
        ready=True,
        status="ready",
        roots=roots,
        scopes={package.key: "user" for package in catalog.packages},
    )
    commands: list[dict[str, Any]] = [
        {
            "name": name,
            "source": "skill",
            "sourceInfo": {
                "origin": "package",
                "scope": "user",
                "baseDir": str(roots["athena"]),
                "path": str(roots["athena"] / "skills" / name.removeprefix("skill:")),
            },
        }
        for name in catalog.required_commands
    ]
    tools: list[dict[str, Any]] = []
    for package in catalog.packages:
        for name in package.tools:
            tools.append(
                {
                    "name": name,
                    "sourceInfo": {
                        "origin": "package",
                        "scope": "user",
                        "baseDir": str(roots[package.key]),
                        "path": str(roots[package.key] / "extensions" / f"{name}.ts"),
                    },
                }
            )
    valid_payload = {
        "commands": commands,
        "reported_commands": commands,
        "active_tools": [tool["name"] for tool in tools],
        "all_tools": tools,
    }
    assert verify_capability_inventory(valid_payload, inventory, catalog).ready is True
    commands[0]["sourceInfo"]["baseDir"] = str(tmp_path / "attacker")

    result = verify_capability_inventory(
        {
            "commands": commands,
            "reported_commands": commands,
            "active_tools": [tool["name"] for tool in tools],
            "all_tools": tools,
        },
        inventory,
        catalog,
    )

    assert result.ready is False
    assert result.status == "capability_provenance_mismatch"


def test_capability_inventory_rejects_non_object_command_and_tool_entries(
    tmp_path: Path,
) -> None:
    """Malformed RPC list elements produce a stable failure instead of a traceback."""
    from hephaestus.agents.pi_plugins import (
        InventoryResult,
        load_pi_package_catalog,
        verify_capability_inventory,
    )

    catalog = load_pi_package_catalog()
    inventory = InventoryResult(
        ready=True,
        status="ready",
        roots={package.key: tmp_path / package.key for package in catalog.packages},
        scopes={package.key: "user" for package in catalog.packages},
    )
    valid_lists: dict[str, Any] = {
        "commands": [],
        "reported_commands": [],
        "active_tools": [],
        "all_tools": [],
    }

    for field in ("commands", "reported_commands", "all_tools"):
        for malformed in (None, "not-an-object"):
            payload = dict(valid_lists)
            payload[field] = [malformed]

            result = verify_capability_inventory(payload, inventory, catalog)

            assert result.ready is False
            assert result.status == "capability_payload_malformed"


def test_parser_exposes_scope_dry_run_json_approval_timeout_and_yes() -> None:
    """The operator CLI exposes every issue-owned safety control."""
    from hephaestus.agents.pi_plugins import build_parser

    args = build_parser().parse_args(
        [
            "--project-local",
            "--dry-run",
            "--json",
            "--yes",
            "--approve",
            "--timeout",
            "17",
            "--pi-dir",
            "/tmp/pi-agent",
        ]
    )

    assert args.project_local is True
    assert args.dry_run is True
    assert args.json_output is True
    assert args.yes is True
    assert args.approve is True
    assert args.timeout == 17.0
    assert args.pi_dir == Path("/tmp/pi-agent")


def test_preflight_runs_inventory_before_rpc_extension(tmp_path: Path) -> None:
    """The capability extension runs without executing ambient Pi extensions."""
    from hephaestus.agents.pi_plugins import (
        ProcessResult,
        load_pi_package_catalog,
        preflight_pi_environment,
    )

    catalog = load_pi_package_catalog()
    executable = _fake_pi_install(tmp_path)
    pi_dir = tmp_path / "pi-home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (pi_dir / "settings.json").parent.mkdir(parents=True)
    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}),
        encoding="utf-8",
    )
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    user_npm_root = pi_dir / "npm" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "0.5.0")
    _write_package(user_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(user_npm_root / "pi-web-access", "pi-web-access", "0.15.0")
    rpc_calls: list[tuple[str, ...]] = []
    ambient_sentinel = tmp_path / "ambient-extension-loaded"
    ambient_extensions = pi_dir / "extensions"
    ambient_extensions.mkdir()
    (ambient_extensions / "sentinel.ts").write_text(
        "export default function () {}", encoding="utf-8"
    )
    isolated_paths: list[Path] = []

    def source_info(package: str, leaf: str) -> dict[str, str]:
        root = {
            "athena": athena_root,
            "pi-subagents": user_npm_root / "pi-subagents",
            "pi-web-access": user_npm_root / "pi-web-access",
        }[package]
        return {
            "origin": "package",
            "scope": "user",
            "baseDir": str(root.resolve()),
            "path": str((root / leaf).resolve()),
        }

    commands = [
        {"name": name, "source": "skill", "sourceInfo": source_info("athena", name)}
        for name in catalog.required_commands
    ]
    tools = [
        {"name": name, "sourceInfo": source_info(package.key, f"{name}.ts")}
        for package in catalog.packages
        for name in package.tools
    ]

    def runner(argv: tuple[str, ...], **kwargs: Any) -> ProcessResult:
        if argv[-1] == "--version":
            return ProcessResult(0, "0.80.2\n", "")
        rpc_calls.append(argv)
        probe_env = kwargs["env"]
        probe_cwd = kwargs["cwd"]
        assert probe_env is not None
        assert probe_cwd is not None
        assert "NPM_CONFIG_PACKAGE_LOCK" not in probe_env
        isolated_agent_dir = Path(probe_env["HOME"]) / ".pi" / "agent"
        isolated_paths.extend((isolated_agent_dir, probe_cwd))
        if isolated_agent_dir == pi_dir or probe_cwd == cwd:
            ambient_sentinel.write_text("loaded", encoding="utf-8")
        configured = json.loads((isolated_agent_dir / "settings.json").read_text(encoding="utf-8"))
        assert configured["packages"] == [
            str(athena_root.resolve()),
            str((user_npm_root / "pi-subagents").resolve()),
            str((user_npm_root / "pi-web-access").resolve()),
        ]
        assert not (isolated_agent_dir / "extensions").exists()
        assert not (probe_cwd / ".pi").exists()
        assert kwargs["keep_stdin_open"] is True
        request = "".join(kwargs["input_text"] or "")
        nonce = json.loads(request.splitlines()[1])["message"].split()[-1]
        payload = json.dumps(
            {
                "nonce": nonce,
                "reported_commands": commands,
                "active_tools": [tool["name"] for tool in tools],
                "all_tools": tools,
            }
        )
        stdout = "\n".join(
            (
                json.dumps(
                    {
                        "type": "response",
                        "id": "hephaestus-commands",
                        "success": True,
                        "data": {"commands": commands},
                    }
                ),
                json.dumps(
                    {
                        "type": "extension_ui_request",
                        "method": "notify",
                        "message": payload,
                    }
                ),
            )
        )
        return ProcessResult(0, stdout, "")

    result = preflight_pi_environment(
        cwd,
        catalog=catalog,
        pi_bin=executable,
        pi_dir=pi_dir,
        runner=runner,
        git_head=lambda _root: catalog.packages[0].pin,
        git_status=lambda _root: "",
    )

    assert result.ready is True
    assert result.status == "ready"
    assert result.executable == executable.resolve()
    assert result.executable_fingerprint is not None
    assert len(rpc_calls) == 1
    assert "--mode" in rpc_calls[0]
    assert "rpc" in rpc_calls[0]
    assert not ambient_sentinel.exists()
    assert all(not path.exists() for path in isolated_paths)


def test_isolated_probe_preserves_verified_package_scopes(tmp_path: Path) -> None:
    """The isolated settings expose each root only through its verified scope."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    roots = {package.key: tmp_path / package.key for package in catalog.packages}
    inventory = pi_plugins.InventoryResult(
        ready=True,
        status="ready",
        roots=roots,
        scopes={
            "athena": "user",
            "pi-subagents": "project",
            "pi-web-access": "project",
        },
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    with pi_plugins._isolated_pi_probe_environment(cwd, inventory, catalog) as (
        probe_cwd,
        probe_env,
        trust,
    ):
        agent_dir = Path(probe_env["HOME"]) / ".pi" / "agent"
        user_settings = json.loads((agent_dir / "settings.json").read_text(encoding="utf-8"))
        project_settings = json.loads(
            (probe_cwd / ".pi" / "settings.json").read_text(encoding="utf-8")
        )

        assert user_settings["packages"] == [str(roots["athena"])]
        assert project_settings["packages"] == [
            str(roots["pi-subagents"]),
            str(roots["pi-web-access"]),
        ]
        assert trust == "--approve"

    assert not agent_dir.exists()
    assert not probe_cwd.exists()
    assert not (cwd / "build").exists()


def test_catalog_rejects_mutable_or_incomplete_pins(tmp_path: Path) -> None:
    """The package authority rejects mutable npm and abbreviated Git references."""
    from hephaestus.agents.pi_plugins import CATALOG_PATH, load_pi_package_catalog

    document = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "catalog.json"
    document["packages"]["athena"]["commit"] = "main"
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        load_pi_package_catalog(path)
    except ValueError as exc:
        assert "immutable Git commit" in str(exc)
    else:
        raise AssertionError("mutable Athena reference was accepted")

    document["packages"]["athena"]["commit"] = "a" * 40
    document["packages"]["pi-subagents"]["version"] = "latest"
    path.write_text(json.dumps(document), encoding="utf-8")
    try:
        load_pi_package_catalog(path)
    except ValueError as exc:
        assert "exact npm version" in str(exc)
    else:
        raise AssertionError("mutable npm version was accepted")


_EOF_CHILD = """
import json
import os
from pathlib import Path
import signal
import sys
import time

root = Path(sys.argv[1])
mode = sys.argv[2]
if os.name == 'posix':
    signal.alarm(20)
if mode != 'missing-startup':
    receipt = {'pid': os.getpid(), 'ppid': os.getppid()}
    if os.name == 'posix':
        receipt.update({'pgid': os.getpgrp(), 'sid': os.getsid(0)})
    pending = root / 'ready.pending'
    pending.write_text(json.dumps(receipt), encoding='utf-8')
    pending.replace(root / 'ready.json')
while not (root / 'release').exists():
    time.sleep(0.01)
os.write(1, b'output-marker\\n')
os.write(2, b'error-marker\\n')
os.close(1)
os.close(2)
(root / 'eof').touch()
if mode.startswith('exit-'):
    time.sleep(0.05)
    os._exit(int(mode.split('-', 1)[1]))
time.sleep(30)
"""

_LINUX_PIDFD = pytest.mark.skipif(
    not sys.platform.startswith("linux")
    or not hasattr(os, "pidfd_open")
    or not hasattr(signal, "pidfd_send_signal"),
    reason="This supervisor requires native Linux pidfd ownership; other hosts are unqualified",
)


class _EofStartupError(RuntimeError):
    """The child did not complete the private startup handshake."""


class _EofSupervisor:
    """Own one fixed child independently of the runner's wait and cleanup code."""

    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.argv = (sys.executable, "-c", _EOF_CHILD, str(root), mode)
        self.env = {"PATH": os.defpath}
        self.process = subprocess.Popen(
            self.argv,
            cwd=root,
            env=self.env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            creationflags=0,
        )
        try:
            self.pidfd = os.pidfd_open(self.process.pid)
        except BaseException:
            # No other waiter can reap this direct child before this cleanup.
            self.process.kill()
            self.process.wait(timeout=2)
            for stream in (self.process.stdout, self.process.stderr):
                if stream is not None:
                    stream.close()
            raise
        self.thread: threading.Thread | None = None
        self.entered = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None
        self.forced_cleanup = False
        self.reaped = False
        self.closed = False
        self.outer_expired = False
        self.elapsed = 0.0

    def wait_for_startup(self, timeout: float = 3.0) -> None:
        """Check the real child identity before allowing it to close output."""
        deadline = time.monotonic() + timeout
        while not (self.root / "ready.json").exists():
            if time.monotonic() >= deadline:
                raise _EofStartupError("child startup receipt is missing")
            time.sleep(0.01)
        receipt = json.loads((self.root / "ready.json").read_text(encoding="utf-8"))
        assert receipt == {
            "pid": self.process.pid,
            "ppid": os.getpid(),
            "pgid": self.process.pid,
            "sid": self.process.pid,
        }
        assert os.getpgid(self.process.pid) == self.process.pid
        assert os.getsid(self.process.pid) == self.process.pid

    def launch(self, argv: tuple[str, ...], **kwargs: Any) -> subprocess.Popen[bytes]:
        """Supply the owned real child without replacing reader or wait behavior."""
        assert not self.entered.is_set(), "runner attempted a second launch"
        assert argv == self.argv
        assert kwargs == {
            "cwd": self.root,
            "env": self.env,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "start_new_session": True,
            "creationflags": 0,
        }
        self.entered.set()
        return self.process

    def run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        wait_only_stub: bool = False,
        outer_timeout: float = 4.0,
    ) -> None:
        """Run the public EOF path under a separate finite supervisor deadline."""
        from hephaestus.agents import pi_plugins

        def invoke() -> None:
            try:
                if wait_only_stub:
                    self.entered.set()
                    self.process.wait()
                else:
                    self.result = pi_plugins.run_bounded_command(
                        self.argv, cwd=self.root, env=self.env, timeout=1.0
                    )
            except BaseException as exc:
                self.error = exc

        self.wait_for_startup()
        # Only launch is substituted. Pipes, EOF, selectors, wait, and results are real.
        # This checks the EOF contract, not production launch ownership.
        with monkeypatch.context() as patch:
            patch.setattr(subprocess, "Popen", self.launch)
            self.thread = threading.Thread(target=invoke, daemon=True)
            started = time.monotonic()
            try:
                self.thread.start()
                assert self.entered.wait(timeout=1), "runner did not accept the owned process"
                (self.root / "release").touch()
                self.thread.join(timeout=max(0.0, started + outer_timeout - time.monotonic()))
                self.outer_expired = self.thread.is_alive()
                self.elapsed = time.monotonic() - started
                assert (self.root / "eof").exists(), "child did not close both output pipes"
            finally:
                self.close()
        if self.error is not None:
            raise AssertionError(
                "runner raised instead of returning a process result"
            ) from self.error

    def close(self) -> None:
        """Stop the exact child, reap it, and confirm that the runner thread stopped."""
        if self.closed:
            return
        try:
            # A pidfd cannot signal a different process after PID reuse. The fixed
            # fixture creates no descendants; its one child owns a separate session.
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(self.pidfd, signal.SIGKILL)
                    self.forced_cleanup = True
            self.process.wait(timeout=2)
            self.reaped = self.process.returncode is not None
            if self.thread is not None:
                self.thread.join(timeout=2)
                assert not self.thread.is_alive(), "runner thread survived independent cleanup"
            assert self.reaped, "owned child was not reaped"
        finally:
            os.close(self.pidfd)
            # Do not wait for a buffered-stream lock held by an unjoined thread.
            # An unjoined thread already makes the cleanup qualification fail.
            if self.thread is None or not self.thread.is_alive():
                for stream in (self.process.stdout, self.process.stderr):
                    if stream is not None:
                        stream.close()
            self.closed = True


@contextmanager
def _owned_eof_child(tmp_path: Path, mode: str = "hang") -> Iterator[_EofSupervisor]:
    """Clean the direct child, including an incomplete startup."""
    owner = _EofSupervisor(tmp_path, mode)
    try:
        yield owner
    finally:
        owner.close()


@_LINUX_PIDFD
def test_runner_supervisor_forced_cleanup_owns_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Qualify cleanup without relying on any runner timeout or termination code."""
    with _owned_eof_child(tmp_path) as owner:
        owner.run(monkeypatch, wait_only_stub=True, outer_timeout=0.25)
    assert owner.outer_expired
    assert owner.forced_cleanup
    assert owner.reaped
    assert owner.thread is not None and not owner.thread.is_alive()
    assert owner.process.returncode == -signal.SIGKILL


@_LINUX_PIDFD
def test_runner_supervisor_rejects_incomplete_startup(tmp_path: Path) -> None:
    """A missing receipt fails setup but leaves no unowned or unreaped child."""
    with pytest.raises(_EofStartupError, match="startup receipt is missing"):
        with _owned_eof_child(tmp_path, "missing-startup") as owner:
            owner.wait_for_startup(timeout=0.25)
    assert owner.forced_cleanup
    assert owner.reaped
    assert owner.thread is None
    assert not (tmp_path / "release").exists()
    assert owner.process.returncode == -signal.SIGKILL


@_LINUX_PIDFD
def test_bounded_runner_deadline_survives_output_eof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """EOF must not remove the command deadline while the real child stays alive."""
    with _owned_eof_child(tmp_path) as owner:
        owner.run(monkeypatch)
    assert owner.reaped
    assert not owner.outer_expired, "public runner exceeded its deadline after output EOF"
    assert not owner.forced_cleanup, "supervisor, not the runner, stopped the child"
    assert owner.result.timed_out
    assert owner.result.returncode != 0
    assert owner.result.stdout == "output-marker\n"
    assert owner.result.stderr == "error-marker\n"
    # One second is harness scheduling tolerance, not another product allowance.
    assert owner.elapsed <= 1.0 + 2.0 + 1.0


@_LINUX_PIDFD
@pytest.mark.parametrize("returncode", [0, 7], ids=["zero", "nonzero"])
def test_bounded_runner_eof_preserves_timely_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    """A timely exit keeps separate output and the child's actual status."""
    with _owned_eof_child(tmp_path, f"exit-{returncode}") as owner:
        owner.run(monkeypatch)
    assert owner.reaped
    assert not owner.outer_expired
    assert not owner.forced_cleanup
    assert not owner.result.timed_out
    assert owner.result.returncode == returncode
    assert owner.result.stdout == "output-marker\n"
    assert owner.result.stderr == "error-marker\n"


_WINDOWS_NATIVE = pytest.mark.skipif(
    sys.platform != "win32", reason="requires native Windows process semantics"
)
_WINDOWS_CREATE_SUSPENDED = 0x00000004
_WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_WINDOWS_PROCESS_SET_QUOTA = 0x0100
_WINDOWS_PROCESS_TERMINATE = 0x0001
_WINDOWS_THREAD_SUSPEND_RESUME = 0x0002
_WINDOWS_TH32CS_SNAPTHREAD = 0x00000004
_WINDOWS_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_int64),
        ("per_job_user_time_limit", ctypes.c_int64),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _WindowsJobIoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_uint64),
        ("write_operation_count", ctypes.c_uint64),
        ("other_operation_count", ctypes.c_uint64),
        ("read_transfer_count", ctypes.c_uint64),
        ("write_transfer_count", ctypes.c_uint64),
        ("other_transfer_count", ctypes.c_uint64),
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _WindowsJobBasicLimitInformation),
        ("io_info", _WindowsJobIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsJobBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("total_user_time", ctypes.c_int64),
        ("total_kernel_time", ctypes.c_int64),
        ("this_period_total_user_time", ctypes.c_int64),
        ("total_page_fault_count", ctypes.c_uint32),
        ("total_processes", ctypes.c_uint32),
        ("active_processes", ctypes.c_uint32),
        ("total_terminated_processes", ctypes.c_uint32),
    ]


class _WindowsThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage_count", ctypes.c_uint32),
        ("thread_id", ctypes.c_uint32),
        ("owner_process_id", ctypes.c_uint32),
        ("base_priority", ctypes.c_int32),
        ("delta_priority", ctypes.c_int32),
        ("flags", ctypes.c_uint32),
    ]


class _WindowsOuterJob:
    """Own a suspended helper and all of its descendants independently."""

    def __init__(self) -> None:
        win_dll = cast(Any, vars(ctypes)["WinDLL"])
        self._kernel32 = win_dll("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        dword = ctypes.c_uint32
        bool_type = ctypes.c_int32
        self._configure_signatures(pointer, dword, bool_type)
        self._handle = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise self._error("CreateJobObjectW")
        limits = _WindowsJobExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = _WINDOWS_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            _WINDOWS_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = self._error("SetInformationJobObject")
            self.close()
            raise error

    def _configure_signatures(self, pointer: Any, dword: Any, bool_type: Any) -> None:
        self._kernel32.CreateJobObjectW.argtypes = [pointer, ctypes.c_wchar_p]
        self._kernel32.CreateJobObjectW.restype = pointer
        self._kernel32.SetInformationJobObject.argtypes = [pointer, dword, pointer, dword]
        self._kernel32.SetInformationJobObject.restype = bool_type
        self._kernel32.OpenProcess.argtypes = [dword, bool_type, dword]
        self._kernel32.OpenProcess.restype = pointer
        self._kernel32.AssignProcessToJobObject.argtypes = [pointer, pointer]
        self._kernel32.AssignProcessToJobObject.restype = bool_type
        self._kernel32.TerminateJobObject.argtypes = [pointer, dword]
        self._kernel32.TerminateJobObject.restype = bool_type
        self._kernel32.QueryInformationJobObject.argtypes = [
            pointer,
            dword,
            pointer,
            dword,
            pointer,
        ]
        self._kernel32.QueryInformationJobObject.restype = bool_type
        self._kernel32.CreateToolhelp32Snapshot.argtypes = [dword, dword]
        self._kernel32.CreateToolhelp32Snapshot.restype = pointer
        self._kernel32.Thread32First.argtypes = [pointer, pointer]
        self._kernel32.Thread32First.restype = bool_type
        self._kernel32.Thread32Next.argtypes = [pointer, pointer]
        self._kernel32.Thread32Next.restype = bool_type
        self._kernel32.OpenThread.argtypes = [dword, bool_type, dword]
        self._kernel32.OpenThread.restype = pointer
        self._kernel32.ResumeThread.argtypes = [pointer]
        self._kernel32.ResumeThread.restype = dword
        self._kernel32.CloseHandle.argtypes = [pointer]
        self._kernel32.CloseHandle.restype = bool_type

    @staticmethod
    def _error(operation: str) -> OSError:
        get_last_error = cast(Any, vars(ctypes)["get_last_error"])
        error = get_last_error()
        return OSError(error, f"{operation} failed with Windows error {error}")

    def assign(self, pid: int) -> None:
        """Assign one exact suspended helper process to this Job Object."""
        process_handle = self._kernel32.OpenProcess(
            _WINDOWS_PROCESS_TERMINATE | _WINDOWS_PROCESS_SET_QUOTA,
            False,
            pid,
        )
        if not process_handle:
            raise self._error("OpenProcess")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
                raise self._error("AssignProcessToJobObject")
        finally:
            self._kernel32.CloseHandle(process_handle)

    def resume(self, pid: int) -> None:
        """Resume the primary thread after Job ownership is established."""
        snapshot = self._kernel32.CreateToolhelp32Snapshot(_WINDOWS_TH32CS_SNAPTHREAD, 0)
        if not snapshot or snapshot == _WINDOWS_INVALID_HANDLE_VALUE:
            raise self._error("CreateToolhelp32Snapshot")
        try:
            entry = _WindowsThreadEntry32()
            entry.size = ctypes.sizeof(entry)
            found = bool(self._kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while found:
                if entry.owner_process_id == pid:
                    thread_handle = self._kernel32.OpenThread(
                        _WINDOWS_THREAD_SUSPEND_RESUME,
                        False,
                        entry.thread_id,
                    )
                    if not thread_handle:
                        raise self._error("OpenThread")
                    try:
                        if self._kernel32.ResumeThread(thread_handle) == 0xFFFFFFFF:
                            raise self._error("ResumeThread")
                    finally:
                        self._kernel32.CloseHandle(thread_handle)
                    return
                found = bool(self._kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
        finally:
            self._kernel32.CloseHandle(snapshot)
        raise OSError(f"suspended process {pid} has no resumable thread")

    def terminate(self) -> None:
        """Terminate the helper and all descendants in this Job Object."""
        if not self._kernel32.TerminateJobObject(self._handle, 1):
            raise self._error("TerminateJobObject")

    def active_processes(self) -> int:
        """Return the number of active processes retained by this Job Object."""
        info = _WindowsJobBasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            _WINDOWS_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
            None,
        ):
            raise self._error("QueryInformationJobObject")
        return int(info.active_processes)

    def close(self) -> None:
        """Close the outer Job handle."""
        if self._handle:
            if not self._kernel32.CloseHandle(self._handle):
                raise self._error("CloseHandle")
            self._handle = None


_WINDOWS_RUNNER_HELPER = """
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
child = Path(sys.argv[2])
mode = sys.argv[3]
pending = root / "helper-ready.pending"
pending.write_text(
    json.dumps({"pid": os.getpid(), "ppid": os.getppid()}), encoding="utf-8"
)
pending.replace(root / "helper-ready.json")
from hephaestus.agents.pi_plugins import run_bounded_command

child_env = {"PATH": os.defpath, "SYSTEMROOT": os.environ["SYSTEMROOT"]}
try:
    result = run_bounded_command(
        (sys.executable, str(child), str(root), mode),
        cwd=root,
        env=child_env,
        timeout=1.0,
    )
except BaseException as exc:
    (root / "runner-error.json").write_text(
        json.dumps({"type": type(exc).__name__}), encoding="utf-8"
    )
    raise
(root / "runner-result.json").write_text(
    json.dumps(
        {
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "timed_out": result.timed_out,
            "output_overflow": result.output_overflow,
        }
    ),
    encoding="utf-8",
)
"""


class _WindowsEofSupervisor:
    """Supervise a real runner and child through an outer Job Object."""

    def __init__(self, root: Path, mode: str) -> None:
        self.root = root
        self.child_path = root / "eof_child.py"
        self.child_path.write_text(_EOF_CHILD, encoding="utf-8")
        self.mode = mode
        self.env = {
            "PATH": os.defpath,
            "PYTHONPATH": str(REPO_ROOT),
            "SYSTEMROOT": os.environ["SYSTEMROOT"],
        }
        self.process: subprocess.Popen[bytes] | None = None
        self.job: _WindowsOuterJob | None = None
        self.result: Any = None
        self.forced_cleanup = False
        self.reaped = False
        self.outer_expired = False
        self.closed = False
        self.elapsed = 0.0
        self.active_processes_after_cleanup: int | None = None

    def start(self) -> None:
        """Assign the suspended helper before it can create the runner child."""
        self.job = _WindowsOuterJob()
        try:
            process = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    _WINDOWS_RUNNER_HELPER,
                    str(self.root),
                    str(self.child_path),
                    self.mode,
                ),
                cwd=self.root,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_WINDOWS_CREATE_SUSPENDED,
            )
            self.process = process
            try:
                self.job.assign(process.pid)
                self.job.resume(process.pid)
            except BaseException:
                process.kill()
                process.wait(timeout=2)
                raise
        except BaseException:
            if self.job is not None:
                self.job.close()
            self.closed = True
            raise

    def wait_for_helper_startup(self, timeout: float = 3.0) -> None:
        """Confirm helper identity after its outer Job ownership exists."""
        process = self.process
        assert process is not None
        deadline = time.monotonic() + timeout
        while not (self.root / "helper-ready.json").exists():
            if time.monotonic() >= deadline:
                raise _EofStartupError("runner helper startup receipt is missing")
            time.sleep(0.01)
        receipt = json.loads((self.root / "helper-ready.json").read_text(encoding="utf-8"))
        assert receipt == {"pid": process.pid, "ppid": os.getpid()}

    def wait_for_child_startup(self, timeout: float = 3.0) -> None:
        """Confirm child identity before allowing it to close output."""
        process = self.process
        assert process is not None
        deadline = time.monotonic() + timeout
        while not (self.root / "ready.json").exists():
            if time.monotonic() >= deadline:
                raise _EofStartupError("child startup receipt is missing")
            time.sleep(0.01)
        try:
            receipt = json.loads((self.root / "ready.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise _EofStartupError("child startup receipt is malformed") from exc
        if receipt.get("ppid") != process.pid:
            raise _EofStartupError("child startup receipt has the wrong helper parent")
        pid = receipt.get("pid")
        if not isinstance(pid, int):
            raise _EofStartupError("child startup receipt has no process id")
        assert pid != process.pid

    def run_runner(self, *, release: bool = True, outer_timeout: float = 4.0) -> None:
        """Run the real runner under a finite outer process deadline."""
        self.start()
        process = self.process
        assert process is not None
        self.wait_for_helper_startup()
        self.wait_for_child_startup()
        if not release:
            try:
                process.wait(timeout=outer_timeout)
            except subprocess.TimeoutExpired:
                self.outer_expired = True
            return
        (self.root / "release").touch()
        started = time.monotonic()
        try:
            process.wait(timeout=outer_timeout)
        except subprocess.TimeoutExpired:
            self.outer_expired = True
        self.elapsed = time.monotonic() - started
        assert (self.root / "eof").exists(), "child did not close both output pipes"
        if not self.outer_expired:
            result_path = self.root / "runner-result.json"
            assert result_path.exists(), "runner did not write a result"
            self.result = SimpleNamespace(**json.loads(result_path.read_text(encoding="utf-8")))

    def close(self) -> None:
        """Terminate the known helper Job and confirm descendant cleanup."""
        if self.closed:
            return
        job = self.job
        process = self.process
        if job is None and process is None:
            self.closed = True
            return
        assert job is not None
        assert process is not None
        deadline = time.monotonic() + 2.0
        try:
            if process.poll() is None or job.active_processes() > 0:
                job.terminate()
                self.forced_cleanup = True
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
            self.reaped = process.returncode is not None
            while job.active_processes() > 0 and time.monotonic() < deadline:
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            self.active_processes_after_cleanup = job.active_processes()
            assert self.reaped, "runner helper was not reaped"
            assert self.active_processes_after_cleanup == 0, (
                "outer Job retained a descendant after cleanup"
            )
        finally:
            job.close()
            self.closed = True


def test_windows_supervisor_preserves_job_setup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Job setup error must survive the native test cleanup path."""
    sentinel = RuntimeError("native Job setup failed")

    def fail_job_setup() -> Any:
        raise sentinel

    monkeypatch.setenv("SYSTEMROOT", "test-system-root")
    monkeypatch.setattr(sys.modules[__name__], "_WindowsOuterJob", fail_job_setup)
    owner = _WindowsEofSupervisor(tmp_path, "hang")
    with pytest.raises(RuntimeError) as caught:
        try:
            owner.start()
        finally:
            owner.close()
    assert caught.value is sentinel


@_WINDOWS_NATIVE
def test_windows_supervisor_forced_cleanup_owns_child(tmp_path: Path) -> None:
    """Qualify outer Job cleanup of a real runner and child."""
    owner = _WindowsEofSupervisor(tmp_path, "hang")
    try:
        owner.run_runner(release=False, outer_timeout=0.25)
    finally:
        owner.close()
    assert owner.outer_expired
    assert owner.forced_cleanup
    assert owner.reaped
    assert owner.active_processes_after_cleanup == 0


@_WINDOWS_NATIVE
def test_windows_supervisor_rejects_incomplete_startup(tmp_path: Path) -> None:
    """A missing child receipt still leaves a known helper Job to terminate."""
    owner = _WindowsEofSupervisor(tmp_path, "missing-startup")
    with pytest.raises(_EofStartupError, match="startup receipt is missing"):
        try:
            owner.start()
            owner.wait_for_helper_startup()
            owner.wait_for_child_startup(timeout=0.25)
        finally:
            owner.close()
    assert owner.forced_cleanup
    assert owner.reaped
    assert owner.active_processes_after_cleanup == 0
    assert not (tmp_path / "release").exists()


@_WINDOWS_NATIVE
def test_bounded_runner_windows_deadline_survives_output_eof(tmp_path: Path) -> None:
    """EOF must not remove the Windows command deadline."""
    owner = _WindowsEofSupervisor(tmp_path, "hang")
    try:
        owner.run_runner()
    finally:
        owner.close()
    assert owner.reaped
    assert not owner.outer_expired, "public runner exceeded its deadline after output EOF"
    assert not owner.forced_cleanup, "supervisor, not the runner, stopped the child"
    assert owner.result.timed_out
    assert owner.result.returncode != 0
    assert owner.result.stdout == "output-marker\n"
    assert owner.result.stderr == "error-marker\n"
    assert owner.active_processes_after_cleanup == 0


@_WINDOWS_NATIVE
@pytest.mark.parametrize("returncode", [0, 7], ids=["zero", "nonzero"])
def test_bounded_runner_windows_eof_preserves_timely_exit(tmp_path: Path, returncode: int) -> None:
    """A timely Windows exit keeps its status and separate output."""
    owner = _WindowsEofSupervisor(tmp_path, f"exit-{returncode}")
    try:
        owner.run_runner()
    finally:
        owner.close()
    assert owner.reaped
    assert not owner.outer_expired
    assert not owner.forced_cleanup
    assert not owner.result.timed_out
    assert owner.result.returncode == returncode
    assert owner.result.stdout == "output-marker\n"
    assert owner.result.stderr == "error-marker\n"
    assert owner.active_processes_after_cleanup == 0


def test_bounded_runner_cleanup_failure_preserves_specific_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unconfirmed exit raises OSError without fabricating a completed result."""
    from hephaestus.agents import pi_plugins

    failure = subprocess.TimeoutExpired("private-command", 2)
    process = Mock(returncode=None, stdout=None, stderr=None)
    process.wait.side_effect = failure
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(pi_plugins, "_terminate_process", Mock())

    def expired_reader(
        child: Any, _input: Any, _timeout: Any, _keep_open: Any, cleanup: Any
    ) -> Any:
        return pi_plugins._wait_for_process_exit(
            child, 0, cleanup, None, timed_out=True, overflow=False
        )

    monkeypatch.setattr(pi_plugins, "_run_posix_process", expired_reader)
    monkeypatch.setattr(pi_plugins, "os", SimpleNamespace(name="posix"))
    with pytest.raises(OSError, match="process cleanup could not be confirmed") as caught:
        pi_plugins.run_bounded_command(("private-command",), env={})
    assert caught.value.__cause__ is failure
    assert process.wait.call_count == 1
    assert 0 <= process.wait.call_args.kwargs["timeout"] <= 2
    assert "private-command" not in str(caught.value)


@pytest.mark.parametrize("platform", ["posix", "windows"])
@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_bounded_runner_cleanup_keeps_original_exception(
    monkeypatch: pytest.MonkeyPatch, platform: str, error_type: type[BaseException]
) -> None:
    """Cleanup uncertainty cannot replace an earlier exception or its cause."""
    from hephaestus.agents import pi_plugins

    cause = LookupError("original cause")
    original = error_type("original operation failed")
    original.__cause__ = cause
    process = Mock(returncode=None, stdout=None, stderr=None)
    process.wait.side_effect = subprocess.TimeoutExpired("private-command", 2)
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(pi_plugins, "_terminate_process", Mock())
    monkeypatch.setattr(pi_plugins, "os", SimpleNamespace(name=platform))
    job = Mock()
    monkeypatch.setattr(pi_plugins, "_WindowsJob", Mock(return_value=job))

    def operation_failure(*_args: Any) -> Any:
        raise original

    reader = "_run_posix_process" if platform == "posix" else "_run_windows_process"
    monkeypatch.setattr(pi_plugins, reader, operation_failure)
    with pytest.raises(error_type) as caught:
        pi_plugins.run_bounded_command(("private-command",), env={"PRIVATE": "secret-value"})
    assert caught.value is original
    assert caught.value.__cause__ is cause
    assert original.__notes__ == [
        "Process cleanup is unconfirmed; inspect retained process ownership."
    ]
    traceback = original.__traceback__
    while traceback is not None and traceback.tb_next is not None:
        traceback = traceback.tb_next
    assert traceback is not None
    assert traceback.tb_frame.f_code.co_name == "operation_failure"
    assert process.wait.call_count == 1
    assert 0 <= process.wait.call_args.kwargs["timeout"] <= 2
    if platform == "windows":
        job.close.assert_called_once_with()


def test_process_cleanup_shares_one_allowance_for_reap_and_readers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Termination, reaping, and both joins consume one aggregate budget."""
    from hephaestus.agents import pi_plugins

    now = [100.0]
    timeouts: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    process = Mock(returncode=-9, stdout=None, stderr=None)

    def terminate(*_args: Any) -> None:
        now[0] += 0.25

    def reap(*, timeout: float) -> int:
        timeouts.append(timeout)
        now[0] += 1.0
        return -9

    def join(*, timeout: float) -> None:
        timeouts.append(timeout)
        now[0] += min(timeout, 0.5)

    process.wait.side_effect = reap
    threads = [threading.Thread(), threading.Thread()]
    for thread in threads:
        monkeypatch.setattr(thread, "join", join)
        monkeypatch.setattr(thread, "is_alive", lambda: False)
    monkeypatch.setattr(pi_plugins, "_terminate_process", terminate)
    cleanup = pi_plugins._ProcessCleanup(threads=threads)
    pi_plugins._complete_process_cleanup(process, cleanup, None, terminate=True)
    assert timeouts == [1.75, 0.75, 0.25]
    assert now[0] == 102.0
    assert cleanup.remaining() == 0


def test_process_cleanup_reports_unjoined_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """A reaped process is not complete cleanup while an output reader remains."""
    from hephaestus.agents import pi_plugins

    process = Mock(returncode=0)
    thread = Mock()
    thread.is_alive.return_value = True
    monkeypatch.setattr(pi_plugins, "_terminate_process", Mock())
    with pytest.raises(OSError, match="process cleanup could not be confirmed") as caught:
        pi_plugins._complete_process_cleanup(
            process, pi_plugins._ProcessCleanup(threads=[thread]), None, terminate=False
        )
    assert isinstance(caught.value.__cause__, TimeoutError)
    process.stdout.close.assert_not_called()
    process.stderr.close.assert_not_called()
    assert 0 <= thread.join.call_args.kwargs["timeout"] <= 2


def test_process_cleanup_retains_group_error_after_direct_child_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reaped direct child cannot conceal a failed group termination."""
    from hephaestus.agents import pi_plugins

    failure = PermissionError("fixed group could not be stopped")
    process = Mock(returncode=-9, stdout=None, stderr=None)
    monkeypatch.setattr(pi_plugins, "_terminate_process", Mock(side_effect=failure))
    with pytest.raises(OSError, match="process cleanup could not be confirmed") as caught:
        pi_plugins._complete_process_cleanup(
            process, pi_plugins._ProcessCleanup(), None, terminate=True
        )
    assert caught.value.__cause__ is failure
    process.kill.assert_called_once_with()
    assert process.wait.call_count == 1


def test_process_cleanup_retains_windows_job_close_error() -> None:
    """A failed Job Object close remains visible after the direct process exits."""
    from hephaestus.agents import pi_plugins

    failure = OSError("fixed Job Object close failed")
    process = Mock(returncode=0, stdout=None, stderr=None)
    job = Mock()
    job.close.side_effect = failure
    with pytest.raises(OSError, match="process cleanup could not be confirmed") as caught:
        pi_plugins._complete_process_cleanup(
            process, pi_plugins._ProcessCleanup(), job, terminate=False
        )
    assert caught.value.__cause__ is failure
    job.close.assert_called_once_with()
    assert process.wait.call_count == 1


def test_windows_reader_eof_keeps_exit_deadline_with_controlled_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Check Windows-path wait policy, without claiming native Windows execution."""
    from hephaestus.agents import pi_plugins

    process = Mock(returncode=None, stdin=None)
    waits: list[float] = []

    def wait(*, timeout: float) -> int:
        waits.append(timeout)
        if len(waits) == 1:
            raise subprocess.TimeoutExpired("fixed-child", timeout)
        process.returncode = 1
        return 1

    def read_pipe(name: str, _stream: Any, events: Any) -> None:
        events.put((name, f"{name}-marker".encode()))
        events.put((name, None))

    process.wait.side_effect = wait
    monkeypatch.setattr(subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(pi_plugins, "os", SimpleNamespace(name="windows"))
    monkeypatch.setattr(pi_plugins, "_read_process_pipe", read_pipe)
    job = Mock()
    monkeypatch.setattr(pi_plugins, "_WindowsJob", Mock(return_value=job))
    result = pi_plugins.run_bounded_command(("fixed-child",), env={}, timeout=1)
    assert result.timed_out
    assert result.returncode == 1
    assert result.stdout == "stdout-marker"
    assert result.stderr == "stderr-marker"
    assert len(waits) == 2
    assert 0 <= waits[0] <= 1
    assert 0 <= waits[1] <= 2
    job.terminate.assert_called_once_with()
    job.close.assert_called_once_with()


def test_bounded_runner_times_out_and_stops_output_overflow() -> None:
    """The real subprocess seam bounds both runtime and captured output."""
    from hephaestus.agents.pi_plugins import run_bounded_command

    timed_out = run_bounded_command(
        (sys.executable, "-c", "import time; time.sleep(10)"), timeout=0.05
    )
    overflow = run_bounded_command(
        (sys.executable, "-c", "import os; os.write(1, b'x' * 1100000)"), timeout=5
    )

    assert timed_out.timed_out is True
    assert timed_out.returncode != 0
    assert overflow.output_overflow is True
    assert len(overflow.stdout.encode()) <= 1_048_576


@skipUnless(sys.platform == "win32", "requires Windows process semantics")
def test_bounded_runner_windows_timeout_terminates_descendants(tmp_path: Path) -> None:
    """A timed-out Windows command cannot leave a spawned installer child running."""
    from hephaestus.agents.pi_plugins import run_bounded_command

    started = tmp_path / "descendant-started"
    sentinel = tmp_path / "descendant-survived"
    child = (
        "import pathlib, sys, time\n"
        "pathlib.Path(sys.argv[1]).write_text('started', encoding='utf-8')\n"
        "time.sleep(1.5)\n"
        "pathlib.Path(sys.argv[2]).write_text('survived', encoding='utf-8')\n"
    )
    parent = (
        "import pathlib, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2], sys.argv[3]])\n"
        "started = pathlib.Path(sys.argv[2])\n"
        "deadline = time.monotonic() + 0.8\n"
        "while not started.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(30)\n"
    )

    result = run_bounded_command(
        (sys.executable, "-c", parent, child, str(started), str(sentinel)), timeout=1.0
    )

    assert result.timed_out is True
    assert started.exists(), "test descendant did not start before the timeout"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not sentinel.exists():
        time.sleep(0.05)
    assert not sentinel.exists(), "timed-out child process survived its parent"


def test_bounded_runner_delivers_stdin_and_keeps_streams_separate() -> None:
    """The real subprocess seam supports RPC input without merging diagnostics."""
    from hephaestus.agents.pi_plugins import run_bounded_command

    result = run_bounded_command(
        (
            sys.executable,
            "-c",
            "import sys; data=sys.stdin.read(); print(data); print('diag', file=sys.stderr)",
        ),
        input_text="request",
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "request\n"
    assert result.stderr == "diag\n"

    rpc_style = run_bounded_command(
        (
            sys.executable,
            "-c",
            "import sys; print(sys.stdin.readline().strip())",
        ),
        input_text="request\n",
        keep_stdin_open=True,
        timeout=5,
    )
    assert rpc_style.returncode == 0
    assert rpc_style.stdout == "request\n"


def test_cli_failure_states_are_distinct_and_actionable(tmp_path: Path) -> None:
    """Malformed, non-zero, timeout, and wrong-manifest states remain distinguishable."""
    from hephaestus.agents.pi_plugins import (
        ProcessResult,
        load_pi_package_catalog,
        probe_pi_cli_identity,
    )

    catalog = load_pi_package_catalog()
    missing = probe_pi_cli_identity(tmp_path / "missing", catalog)
    wrong_manifest = _fake_pi_install(tmp_path / "wrong", version="0.80.1")
    wrong = probe_pi_cli_identity(wrong_manifest, catalog)
    executable = _fake_pi_install(tmp_path / "right")

    def result(stdout: str = "", *, returncode: int = 0, timed_out: bool = False) -> Any:
        return lambda *_args, **_kwargs: ProcessResult(
            returncode, stdout, "failure" if returncode else "", timed_out=timed_out
        )

    malformed = probe_pi_cli_identity(executable, catalog, runner=result("version 0.80.2 extra"))
    failed = probe_pi_cli_identity(executable, catalog, runner=result(returncode=1))
    timeout = probe_pi_cli_identity(executable, catalog, runner=result(timed_out=True))

    assert missing.status == "pi_cli_missing"
    assert wrong.status == "pi_cli_version_mismatch"
    assert malformed.status == "pi_cli_version_malformed"
    assert failed.status == "pi_cli_probe_failed"
    assert timeout.status == "pi_cli_probe_timeout"
    assert catalog.pi.npm_spec in missing.remediation


def test_installer_safe_defaults_confirmation_and_partial_state(tmp_path: Path) -> None:
    """Non-interactive mutation requires consent and reports retained partial progress."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    executable = _fake_pi_install(tmp_path)
    confirmation = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(json_output=True),
        catalog=catalog,
        pi_bin=executable,
    )
    calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

    def runner(argv: tuple[str, ...], **kwargs: Any) -> pi_plugins.ProcessResult:
        calls.append((argv, kwargs.get("env")))
        if argv[-1] == "--version":
            return pi_plugins.ProcessResult(0, "0.80.2\n", "")
        if "pi-subagents" in argv[2]:
            return pi_plugins.ProcessResult(1, "", "registry unavailable")
        return pi_plugins.ProcessResult(0, "installed", "")

    partial = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )

    assert confirmation.status == "confirmation_required"
    assert partial.status == "install_failed"
    assert [state.status for state in partial.packages] == ["installed", "failed", "planned"]
    install_env = calls[1][1]
    assert install_env is not None
    assert install_env["NPM_CONFIG_IGNORE_SCRIPTS"] == "true"
    assert install_env["NPM_CONFIG_PACKAGE_LOCK"] == "false"
    assert install_env["GIT_TERMINAL_PROMPT"] == "0"
    npm_install_env = calls[2][1]
    assert npm_install_env is not None
    assert "NPM_CONFIG_PACKAGE_LOCK" not in npm_install_env


def test_pi_child_environment_honors_the_operator_agent_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Install and preflight subprocesses share the selected Pi configuration root."""
    from hephaestus.agents import pi_plugins

    pi_dir = tmp_path / "pi-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "poison"))

    environment = pi_plugins._pi_child_env(pi_dir=pi_dir)

    assert environment["PI_CODING_AGENT_DIR"] == str(pi_dir)
    assert "NPM_CONFIG_PACKAGE_LOCK" not in environment


def test_installer_rejects_invalid_controls_and_reports_timeout(tmp_path: Path) -> None:
    """Invalid trust/timeout controls and a package timeout have stable states."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    executable = _fake_pi_install(tmp_path)
    assert (
        pi_plugins.install_pi_plugins(
            pi_plugins.InstallOptions(timeout=0), catalog=catalog, pi_bin=executable
        ).status
        == "invalid_timeout"
    )
    assert (
        pi_plugins.install_pi_plugins(
            pi_plugins.InstallOptions(approve=True), catalog=catalog, pi_bin=executable
        ).status
        == "approve_requires_project_local"
    )

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> pi_plugins.ProcessResult:
        if argv[-1] == "--version":
            return pi_plugins.ProcessResult(0, "0.80.2\n", "")
        return pi_plugins.ProcessResult(-9, "", "", timed_out=True)

    timeout = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )
    assert timeout.status == "install_timeout"
    assert timeout.packages[0].status == "failed"


def test_project_trust_modes_never_claim_persisted_approval(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Project no-approve is retained-but-not-ready; approve is one-process only."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    executable = _fake_pi_install(tmp_path)

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> pi_plugins.ProcessResult:
        return pi_plugins.ProcessResult(0, "0.80.2\n" if argv[-1] == "--version" else "", "")

    unapproved = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True, project_local=True),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )
    monkeypatch.setattr(
        pi_plugins,
        "preflight_pi_environment",
        Mock(return_value=pi_plugins.PiPreflightResult.ready_result()),
    )
    approved = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True, project_local=True, approve=True),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )

    assert unapproved.status == "installed_unapproved"
    assert all(state.status == "installed" for state in unapproved.packages)
    assert approved.ready is True
    assert approved.approval_persisted is False


def test_inventory_rejects_malformed_settings_and_symlink_escape(tmp_path: Path) -> None:
    """Static inventory fails before extension execution on settings or path attacks."""
    from hephaestus.agents.pi_plugins import inspect_pi_package_inventory, load_pi_package_catalog

    catalog = load_pi_package_catalog()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    pi_dir = tmp_path / "pi-home"
    pi_dir.mkdir()
    (pi_dir / "settings.json").write_text('{"packages": "wrong"}', encoding="utf-8")
    malformed = inspect_pi_package_inventory(cwd, catalog, pi_dir=pi_dir)
    assert malformed.status == "package_settings_invalid"

    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    git_parent = pi_dir / "git" / "github.com" / "HomericIntelligence"
    git_parent.mkdir(parents=True)
    (git_parent / "Athena").symlink_to(outside, target_is_directory=True)
    escaped = inspect_pi_package_inventory(cwd, catalog, pi_dir=pi_dir)
    assert escaped.status == "package_root_invalid"
    assert "escapes" in escaped.detail


def test_settings_accept_object_form_but_reject_disabled_or_duplicate(tmp_path: Path) -> None:
    """Pi's documented object form remains strict about enablement and duplicates."""
    from hephaestus.agents import pi_plugins

    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps({"packages": [{"source": "npm:example@1.0.0", "enabled": True}]}),
        encoding="utf-8",
    )
    assert pi_plugins._settings_packages(settings) == ("npm:example@1.0.0",)
    settings.write_text(
        json.dumps({"packages": [{"source": "npm:example@1.0.0", "enabled": False}]}),
        encoding="utf-8",
    )
    try:
        pi_plugins._settings_packages(settings)
    except ValueError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("disabled package was accepted")
    settings.write_text(
        json.dumps({"packages": ["npm:example@1.0.0", "npm:example@1.0.0"]}),
        encoding="utf-8",
    )
    try:
        pi_plugins._settings_packages(settings)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate package was accepted")


def test_rpc_parser_requires_top_level_notify_and_correlation() -> None:
    """Only the documented top-level RPC notification and matching IDs are accepted."""
    from hephaestus.agents import pi_plugins

    nonce = "a" * 32
    payload = {"nonce": nonce, "reported_commands": [], "active_tools": [], "all_tools": []}
    good = "\n".join(
        (
            json.dumps(
                {
                    "type": "response",
                    "id": "hephaestus-commands",
                    "success": True,
                    "data": {"commands": []},
                }
            ),
            json.dumps(
                {"type": "extension_ui_request", "method": "notify", "message": json.dumps(payload)}
            ),
        )
    )
    assert pi_plugins._parse_capability_rpc(good, nonce)["nonce"] == nonce

    nested = good.replace('"message":', '"params": {"message":', 1).replace("}}", "}}}", 1)
    try:
        pi_plugins._parse_capability_rpc(nested, nonce)
    except ValueError:
        pass
    else:
        raise AssertionError("undocumented nested notify payload was accepted")


def test_global_no_approve_inventory_ignores_project_package_shadow(tmp_path: Path) -> None:
    """Global verification cannot be satisfied or shadowed by unapproved project settings."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    pi_dir = tmp_path / "pi-home"
    cwd = tmp_path / "repo"
    cwd.mkdir()
    (pi_dir / "settings.json").parent.mkdir(parents=True)
    (pi_dir / "settings.json").write_text(
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    (cwd / ".pi").mkdir()
    (cwd / ".pi" / "settings.json").write_text(
        json.dumps({"packages": ["npm:pi-subagents@9.9.9"]}), encoding="utf-8"
    )
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    user_npm_root = pi_dir / "npm" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "0.5.0")
    _write_package(user_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(user_npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = pi_plugins.inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=lambda _root: catalog.packages[0].pin,
        git_status=lambda _root: "",
        include_project=False,
    )

    assert result.ready is True


def test_preflight_classifies_capability_process_failures(tmp_path: Path, monkeypatch: Any) -> None:
    """Dynamic probe timeout, process failure, and malformed JSON remain distinct."""
    from hephaestus.agents import pi_plugins

    catalog = pi_plugins.load_pi_package_catalog()
    executable = tmp_path / "pi"
    executable.write_text("", encoding="utf-8")
    identity = pi_plugins.PiCliIdentity(True, "ready", executable, tmp_path, "0.80.2", "")
    inventory = pi_plugins.InventoryResult(
        True,
        "ready",
        {package.key: tmp_path / package.key for package in catalog.packages},
        {package.key: "user" for package in catalog.packages},
    )
    monkeypatch.setattr(pi_plugins, "probe_pi_cli_identity", Mock(return_value=identity))
    monkeypatch.setattr(pi_plugins, "inspect_pi_package_inventory", Mock(return_value=inventory))
    cases = (
        (pi_plugins.ProcessResult(-9, "", "", timed_out=True), "capability_probe_timeout"),
        (pi_plugins.ProcessResult(1, "", "failed"), "capability_probe_failed"),
        (pi_plugins.ProcessResult(0, "not-json", ""), "capability_payload_malformed"),
    )
    for process_result, expected in cases:
        result = pi_plugins.preflight_pi_environment(
            tmp_path,
            catalog=catalog,
            pi_bin=executable,
            runner=Mock(return_value=process_result),
        )
        assert result.status == expected


def test_cli_main_declining_interactive_confirmation_does_not_install(
    monkeypatch: Any, capsys: Any
) -> None:
    """Answering no must return before executable probing or package mutation."""
    from hephaestus.agents import pi_plugins

    monkeypatch.setattr(sys, "stdin", Mock(isatty=Mock(return_value=True)))
    monkeypatch.setattr("builtins.input", Mock(return_value="n"))
    monkeypatch.setattr(shutil, "which", Mock(return_value="/tmp/pi"))
    probe = Mock(side_effect=AssertionError("declined installation reached Pi probing"))
    monkeypatch.setattr(pi_plugins, "probe_pi_cli_identity", probe)

    assert pi_plugins.main([]) == 2
    assert "confirmation_required" in capsys.readouterr().out
    probe.assert_not_called()


def test_cli_main_emits_machine_readable_report_and_stable_exit(
    monkeypatch: Any, capsys: Any
) -> None:
    """The installed entry point exposes package states and non-persisted approval."""
    from hephaestus.agents import pi_plugins

    report = pi_plugins.InstallReport(
        False,
        "installed_unapproved",
        (("pi", "--version"),),
        "verify with approval",
        (pi_plugins.PiPackageState("athena", "git:example@" + "a" * 40, "installed"),),
    )
    monkeypatch.setattr(pi_plugins, "install_pi_plugins", Mock(return_value=report))

    assert pi_plugins.main(["--json", "--yes", "--project-local"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "installed_unapproved"
    assert payload["packages"][0]["status"] == "installed"
    assert payload["approval_persisted"] is False


def test_cli_main_localizes_authored_install_detail(monkeypatch: Any, capsys: Any) -> None:
    """Translate an authored installer detail only in human output."""
    from hephaestus.agents import pi_plugins
    from hephaestus.cli.localization import using_localizer

    report = pi_plugins.InstallReport(False, "confirmation_required", (), "rerun with --yes")
    monkeypatch.setattr(pi_plugins, "install_pi_plugins", Mock(return_value=report))

    with using_localizer({"rerun with --yes": "réexécuter avec --yes"}):
        assert pi_plugins.main([]) == 2

    assert "réexécuter avec --yes" in capsys.readouterr().err


def test_cli_main_localizes_preflight_remediation(monkeypatch: Any, capsys: Any) -> None:
    """Translate the authored preflight instruction and retain the status value."""
    from hephaestus.agents import pi_plugins
    from hephaestus.cli.localization import using_localizer

    remediation = "Run hephaestus-install-pi-plugins --global --yes --no-approve"
    report = pi_plugins.InstallReport(
        False,
        "preflight_failed",
        (),
        f"Pi package preflight failed: package_missing. {remediation}",
    )
    monkeypatch.setattr(pi_plugins, "install_pi_plugins", Mock(return_value=report))
    catalog = {
        "Pi package preflight failed: %(status)s%(detail)s. %(remediation)s": (
            "Échec du contrôle Pi : %(status)s%(detail)s. %(remediation)s"
        ),
        remediation: "Exécutez hephaestus-install-pi-plugins --global --yes --no-approve",
    }

    with using_localizer(catalog):
        assert pi_plugins.main([]) == 1

    error = capsys.readouterr().err
    assert "Échec du contrôle Pi : package_missing." in error
    assert "Exécutez hephaestus-install-pi-plugins" in error
