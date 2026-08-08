"""Behavior tests for the Pi package bootstrap and preflight contract."""

from __future__ import annotations

import json
import shutil
import sys
import time
import tomllib
from pathlib import Path
from typing import Any
from unittest import skipUnless
from unittest.mock import Mock

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
    assert catalog["packages"]["athena"]["commit"] == ("496815b00f6fb4c8e97466489371b364d52588b5")
    assert catalog["packages"]["athena"]["name"] == "@homericintelligence/athena"
    assert catalog["packages"]["pi-subagents"]["version"] == "0.37.2"
    assert catalog["packages"]["pi-web-access"]["version"] == "0.15.0"


def test_catalog_builds_only_immutable_native_install_specs() -> None:
    """The installer derives argv from validated catalog pins, not literals."""
    from hephaestus.agents.pi_plugins import load_pi_package_catalog

    catalog = load_pi_package_catalog()

    assert catalog.install_specs == (
        "git:github.com/HomericIntelligence/Athena@496815b00f6fb4c8e97466489371b364d52588b5",
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
        "git:github.com/HomericIntelligence/Athena@496815b00f6fb4c8e97466489371b364d52588b5",
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
    calls: list[tuple[str, ...]] = []

    def runner(argv: tuple[str, ...], **_kwargs: Any) -> pi_plugins.ProcessResult:
        calls.append(argv)
        if argv[-1] == "--version":
            return pi_plugins.ProcessResult(0, "0.80.2\n", "")
        return pi_plugins.ProcessResult(0, "installed\n", "")

    ready = pi_plugins.PiPreflightResult.ready_result()
    preflight = Mock(return_value=ready)
    monkeypatch.setattr(pi_plugins, "preflight_pi_environment", preflight)

    report = pi_plugins.install_pi_plugins(
        pi_plugins.InstallOptions(yes=True),
        catalog=catalog,
        pi_bin=executable,
        runner=runner,
    )

    assert report.ready is True
    assert report.status == "ready"
    assert len(calls) == 4
    assert preflight.call_args.kwargs["trust_override"] == "--no-approve"


def _write_package(root: Path, name: str, version: str) -> None:
    root.mkdir(parents=True)
    (root / "package.json").write_text(
        json.dumps({"name": name, "version": version}), encoding="utf-8"
    )


def test_inventory_respects_pi_coding_agent_dir_and_exact_package_identity(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Global npm packages resolve through npm rather than the Pi settings root."""
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
    global_npm_root = tmp_path / "npm-global" / "lib" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "v0.4.0")
    _write_package(global_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(global_npm_root / "pi-web-access", "pi-web-access", "0.15.0")
    npm_root = Mock(return_value=pi_plugins.ProcessResult(0, f"{global_npm_root}\n", ""))
    monkeypatch.setattr(pi_plugins, "run_bounded_command", npm_root)

    result = pi_plugins.inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=lambda root: catalog.packages[0].pin if root == athena_root else "",
    )

    assert result.ready is True
    assert result.status == "ready"
    assert result.roots["athena"] == athena_root.resolve()
    assert result.roots["pi-subagents"] == (global_npm_root / "pi-subagents").resolve()
    assert set(result.scopes.values()) == {"user"}
    npm_root.assert_called_once_with(("npm", "root", "-g"), timeout=30)


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
    _write_package(athena_root, "@homericintelligence/athena", "v0.4.0")
    _write_package(project_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(project_npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=tmp_path / "pi-home",
        git_head=lambda root: catalog.packages[0].pin if root == athena_root else "",
    )

    assert result.ready is True
    assert result.roots["pi-web-access"] == (project_npm_root / "pi-web-access").resolve()
    assert set(result.scopes.values()) == {"project"}


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
        ["--project-local", "--dry-run", "--json", "--yes", "--approve", "--timeout", "17"]
    )

    assert args.project_local is True
    assert args.dry_run is True
    assert args.json_output is True
    assert args.yes is True
    assert args.approve is True
    assert args.timeout == 17.0


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
        json.dumps({"packages": list(catalog.install_specs)}), encoding="utf-8"
    )
    athena_root = pi_dir / "git" / "github.com" / "HomericIntelligence" / "Athena"
    global_npm_root = tmp_path / "npm-global" / "lib" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "v0.4.0")
    _write_package(global_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(global_npm_root / "pi-web-access", "pi-web-access", "0.15.0")
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
            "pi-subagents": global_npm_root / "pi-subagents",
            "pi-web-access": global_npm_root / "pi-web-access",
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
        if argv == ("npm", "root", "-g"):
            return ProcessResult(0, f"{global_npm_root}\n", "")
        rpc_calls.append(argv)
        probe_env = kwargs["env"]
        probe_cwd = kwargs["cwd"]
        assert probe_env is not None
        assert probe_cwd is not None
        isolated_agent_dir = Path(probe_env["PI_CODING_AGENT_DIR"])
        isolated_paths.extend((isolated_agent_dir, probe_cwd))
        if isolated_agent_dir == pi_dir or probe_cwd == cwd:
            ambient_sentinel.write_text("loaded", encoding="utf-8")
        configured = json.loads((isolated_agent_dir / "settings.json").read_text(encoding="utf-8"))
        assert configured["packages"] == [
            str(athena_root.resolve()),
            str((global_npm_root / "pi-subagents").resolve()),
            str((global_npm_root / "pi-web-access").resolve()),
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
    )

    assert result.ready is True
    assert result.status == "ready"
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
        agent_dir = Path(probe_env["PI_CODING_AGENT_DIR"])
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
    assert install_env["GIT_TERMINAL_PROMPT"] == "0"


def test_pi_child_environment_honors_the_operator_agent_directory(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Install and preflight subprocesses share the selected Pi configuration root."""
    from hephaestus.agents import pi_plugins

    pi_dir = tmp_path / "pi-agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(pi_dir))

    assert pi_plugins._pi_child_env()["PI_CODING_AGENT_DIR"] == str(pi_dir)


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
    global_npm_root = tmp_path / "npm-global" / "lib" / "node_modules"
    _write_package(athena_root, "@homericintelligence/athena", "v0.4.0")
    _write_package(global_npm_root / "pi-subagents", "pi-subagents", "0.37.2")
    _write_package(global_npm_root / "pi-web-access", "pi-web-access", "0.15.0")

    result = pi_plugins.inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=lambda _root: catalog.packages[0].pin,
        include_project=False,
        runner=Mock(return_value=pi_plugins.ProcessResult(0, f"{global_npm_root}\n", "")),
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
