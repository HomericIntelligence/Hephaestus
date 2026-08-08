"""Install and preflight the catalog-pinned Pi package capability set.

This module belongs to the provider runtime library boundary.  It deliberately
does not import :mod:`hephaestus.automation`; automation may depend on this
module, never the reverse.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from hephaestus.cli.utils import add_version_arg

CATALOG_PATH = Path(__file__).with_name("pi_package_catalog.json")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_VERSION_OUTPUT_RE = re.compile(r"(?:pi\s+|v)?([0-9]+\.[0-9]+\.[0-9]+)\n?\Z")
_MAX_OUTPUT_BYTES = 1_048_576


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return cast(dict[str, Any], value)


def _text(document: dict[str, Any], name: str, context: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{context}.{name} must be a non-empty string")
    return value


def _strings(document: dict[str, Any], name: str, context: str) -> tuple[str, ...]:
    value = document.get(name)
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{context}.{name} must be a non-empty string array")
    values = cast(list[str], value)
    if len(values) != len(set(values)):
        raise ValueError(f"{context}.{name} must not contain duplicates")
    return tuple(values)


@dataclass(frozen=True)
class PiCliRequirement:
    """Exact npm identity required for the Pi executable."""

    npm_name: str
    version: str

    @property
    def npm_spec(self) -> str:
        """Return the immutable npm installation specification."""
        return f"{self.npm_name}@{self.version}"


@dataclass(frozen=True)
class PiPackageRequirement:
    """One exact package source and its required capabilities."""

    key: str
    kind: str
    identity: str
    pin: str
    manifest_name: str
    manifest_version: str
    commands: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()

    @property
    def install_spec(self) -> str:
        """Return the native immutable Pi package source."""
        return f"{self.kind}:{self.identity}@{self.pin}"


@dataclass(frozen=True)
class PiPackageCatalog:
    """Strictly validated Pi CLI and companion-package contract."""

    schema_version: int
    pi: PiCliRequirement
    packages: tuple[PiPackageRequirement, ...]

    @property
    def install_specs(self) -> tuple[str, ...]:
        """Return the ordered native Pi package specifications."""
        return tuple(package.install_spec for package in self.packages)

    @property
    def required_commands(self) -> tuple[str, ...]:
        """Return all catalogued command capabilities."""
        return tuple(command for package in self.packages for command in package.commands)

    @property
    def required_tools(self) -> tuple[str, ...]:
        """Return all catalogued tool capabilities."""
        return tuple(tool for package in self.packages for tool in package.tools)


def load_pi_package_catalog(path: Path = CATALOG_PATH) -> PiPackageCatalog:
    """Load and strictly validate the distributable package catalog."""
    try:
        root = _object(json.loads(path.read_text(encoding="utf-8")), "catalog")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load Pi package catalog {path}: {exc}") from exc
    if root.get("schema_version") != 1:
        raise ValueError("catalog.schema_version must equal 1")

    compatibility = _object(root.get("compatibility"), "catalog.compatibility")
    pi_data = _object(compatibility.get("pi"), "catalog.compatibility.pi")
    pi = PiCliRequirement(
        npm_name=_text(pi_data, "npm_name", "catalog.compatibility.pi"),
        version=_text(pi_data, "version", "catalog.compatibility.pi"),
    )
    if (
        pi.npm_name != "@earendil-works/pi-coding-agent"
        or _VERSION_RE.fullmatch(pi.version) is None
    ):
        raise ValueError("catalog Pi CLI identity must be an exact approved npm version")

    package_data = _object(root.get("packages"), "catalog.packages")
    if tuple(package_data) != ("athena", "pi-subagents", "pi-web-access"):
        raise ValueError("catalog packages must contain the ordered approved package set")

    athena = _object(package_data["athena"], "catalog.packages.athena")
    athena_commit = _text(athena, "commit", "catalog.packages.athena")
    if athena.get("kind") != "git" or _COMMIT_RE.fullmatch(athena_commit) is None:
        raise ValueError("Athena must use an immutable Git commit")
    packages = [
        PiPackageRequirement(
            key="athena",
            kind="git",
            identity=_text(athena, "repository", "catalog.packages.athena"),
            pin=athena_commit,
            manifest_name=_text(athena, "name", "catalog.packages.athena"),
            manifest_version=_text(athena, "version", "catalog.packages.athena"),
            commands=_strings(athena, "commands", "catalog.packages.athena"),
        )
    ]
    for key in ("pi-subagents", "pi-web-access"):
        item = _object(package_data[key], f"catalog.packages.{key}")
        version = _text(item, "version", f"catalog.packages.{key}")
        if item.get("kind") != "npm" or _VERSION_RE.fullmatch(version) is None:
            raise ValueError(f"{key} must use an exact npm version")
        packages.append(
            PiPackageRequirement(
                key=key,
                kind="npm",
                identity=_text(item, "name", f"catalog.packages.{key}"),
                pin=version,
                manifest_name=_text(item, "name", f"catalog.packages.{key}"),
                manifest_version=version,
                tools=_strings(item, "tools", f"catalog.packages.{key}"),
            )
        )
    return PiPackageCatalog(schema_version=1, pi=pi, packages=tuple(packages))


@dataclass(frozen=True)
class ProcessResult:
    """Bounded child-process result used by injectable bootstrap seams."""

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    output_overflow: bool = False


class CommandRunner(Protocol):
    """Callable boundary for argv-only subprocess execution."""

    def __call__(
        self,
        argv: tuple[str, ...],
        *,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 30.0,
        input_text: str | None = None,
        keep_stdin_open: bool = False,
    ) -> ProcessResult:
        """Execute one bounded argument vector."""


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - exercised on Windows CI
            process.kill()
    except ProcessLookupError:
        # The child may exit between the liveness check and signal delivery.
        pass


def _read_process_pipe(
    name: str, stream: Any, events: queue.Queue[tuple[str, bytes | None]]
) -> None:
    while chunk := os.read(stream.fileno(), 65_536):
        events.put((name, chunk))
    events.put((name, None))


def _write_process_input(
    process: subprocess.Popen[bytes], input_text: str | None, keep_stdin_open: bool
) -> None:
    if process.stdin is None:
        return
    if input_text is not None:
        process.stdin.write(input_text.encode())
        process.stdin.flush()
    if not keep_stdin_open:
        process.stdin.close()


def _close_process_input(process: subprocess.Popen[bytes]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()


def _run_windows_process(
    process: subprocess.Popen[bytes],
    input_text: str | None,
    timeout: float,
    keep_stdin_open: bool,
) -> ProcessResult:  # pragma: no cover - exercised on Windows CI
    _write_process_input(process, input_text, keep_stdin_open)
    stdout_pipe = cast(Any, process.stdout)
    stderr_pipe = cast(Any, process.stderr)
    events: queue.Queue[tuple[str, bytes | None]] = queue.Queue()
    threads = [
        threading.Thread(target=_read_process_pipe, args=(name, stream, events), daemon=True)
        for name, stream in (("stdout", stdout_pipe), ("stderr", stderr_pipe))
    ]
    for thread in threads:
        thread.start()
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    finished = 0
    total = 0
    timed_out = False
    overflow = False
    while finished < 2:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process(process)
            break
        try:
            name, chunk = events.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            continue
        if chunk is None:
            finished += 1
            continue
        available = max(0, _MAX_OUTPUT_BYTES - total)
        buffers[name].extend(chunk[:available])
        total += len(chunk)
        if total > _MAX_OUTPUT_BYTES:
            overflow = True
            _terminate_process(process)
            break
    _close_process_input(process)
    process.wait()
    for thread in threads:
        thread.join(timeout=1)
    return ProcessResult(
        process.returncode or (-9 if timed_out or overflow else 0),
        buffers["stdout"].decode(errors="replace"),
        buffers["stderr"].decode(errors="replace"),
        timed_out=timed_out,
        output_overflow=overflow,
    )


def _run_posix_process(
    process: subprocess.Popen[bytes],
    input_text: str | None,
    timeout: float,
    keep_stdin_open: bool,
) -> ProcessResult:
    _write_process_input(process, input_text, keep_stdin_open)
    stdout_pipe = cast(Any, process.stdout)
    stderr_pipe = cast(Any, process.stderr)
    streams = {stdout_pipe: bytearray(), stderr_pipe: bytearray()}
    selector = selectors.DefaultSelector()
    for stream in streams:
        selector.register(stream, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    timed_out = False
    overflow = False
    total = 0
    while selector.get_map():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            _terminate_process(process)
            break
        for key, _mask in selector.select(min(remaining, 0.1)):
            stream = cast(Any, key.fileobj)
            chunk = os.read(stream.fileno(), 65_536)
            if not chunk:
                selector.unregister(stream)
                stream.close()
                continue
            available = max(0, _MAX_OUTPUT_BYTES - total)
            streams[stream].extend(chunk[:available])
            total += len(chunk)
            if total > _MAX_OUTPUT_BYTES:
                overflow = True
                _terminate_process(process)
                break
        if overflow:
            break
    selector.close()
    _close_process_input(process)
    for stream in streams:
        if not stream.closed:
            stream.close()
    process.wait()
    stdout = streams[stdout_pipe].decode(errors="replace")
    stderr = streams[stderr_pipe].decode(errors="replace")
    return ProcessResult(
        process.returncode or (-9 if timed_out or overflow else 0),
        stdout,
        stderr,
        timed_out=timed_out,
        output_overflow=overflow,
    )


def run_bounded_command(
    argv: tuple[str, ...],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    input_text: str | None = None,
    keep_stdin_open: bool = False,
) -> ProcessResult:
    """Run one argv without a shell and kill it on timeout or output overflow."""
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    if os.name != "posix":
        return _run_windows_process(process, input_text, timeout, keep_stdin_open)
    return _run_posix_process(process, input_text, timeout, keep_stdin_open)


@dataclass(frozen=True)
class PiCliIdentity:
    """Result of binding an executable to the approved npm identity."""

    ready: bool
    status: str
    executable: Path | None
    package_root: Path | None
    version: str | None
    remediation: str
    detail: str = ""


def _pi_remediation(catalog: PiPackageCatalog) -> str:
    return f"npm install -g --ignore-scripts {catalog.pi.npm_spec}"


def _failed_cli_identity(
    status: str,
    catalog: PiPackageCatalog,
    *,
    executable: Path | None = None,
    package_root: Path | None = None,
    detail: str = "",
) -> PiCliIdentity:
    return PiCliIdentity(
        ready=False,
        status=status,
        executable=executable,
        package_root=package_root,
        version=None,
        remediation=_pi_remediation(catalog),
        detail=detail,
    )


def _find_pi_manifest(executable: Path) -> tuple[Path, dict[str, Any]] | None:
    for parent in (executable.parent, *executable.parents[:11]):
        manifest = parent / "package.json"
        try:
            document = _object(json.loads(manifest.read_text(encoding="utf-8")), "Pi package")
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return parent, document
    return None


def probe_pi_cli_identity(
    pi_bin: Path,
    catalog: PiPackageCatalog,
    *,
    runner: CommandRunner = run_bounded_command,
    timeout: float = 30.0,
) -> PiCliIdentity:
    """Verify exact executable ownership and anchored ``pi --version`` output."""
    try:
        executable = pi_bin.expanduser().resolve(strict=True)
    except OSError as exc:
        return _failed_cli_identity("pi_cli_missing", catalog, detail=str(exc))
    manifest_result = _find_pi_manifest(executable)
    if manifest_result is None:
        return _failed_cli_identity(
            "pi_cli_identity_mismatch",
            catalog,
            executable=executable,
            detail="package.json missing",
        )
    package_root, manifest = manifest_result
    if manifest.get("name") != catalog.pi.npm_name:
        return _failed_cli_identity(
            "pi_cli_identity_mismatch",
            catalog,
            executable=executable,
            package_root=package_root,
            detail="npm manifest identity does not match the catalog",
        )
    if manifest.get("version") != catalog.pi.version:
        return _failed_cli_identity(
            "pi_cli_version_mismatch",
            catalog,
            executable=executable,
            package_root=package_root,
            detail=(
                f"expected {catalog.pi.version}, observed manifest {manifest.get('version')!r}"
            ),
        )
    try:
        result = runner((str(executable), "--version"), timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed_cli_identity(
            "pi_cli_probe_failed",
            catalog,
            executable=executable,
            package_root=package_root,
            detail=str(exc),
        )
    if result.timed_out:
        return _failed_cli_identity(
            "pi_cli_probe_timeout", catalog, executable=executable, package_root=package_root
        )
    if result.returncode != 0 or result.output_overflow:
        return _failed_cli_identity(
            "pi_cli_probe_failed",
            catalog,
            executable=executable,
            package_root=package_root,
            detail=(result.stderr or result.stdout)[:1000],
        )
    match = _VERSION_OUTPUT_RE.fullmatch(result.stdout)
    if match is None:
        return _failed_cli_identity(
            "pi_cli_version_malformed", catalog, executable=executable, package_root=package_root
        )
    version = match.group(1)
    if version != catalog.pi.version:
        return _failed_cli_identity(
            "pi_cli_version_mismatch",
            catalog,
            executable=executable,
            package_root=package_root,
            detail=f"expected {catalog.pi.version}, observed {version}",
        )
    return PiCliIdentity(True, "ready", executable, package_root, version, "")


@dataclass(frozen=True)
class InstallOptions:
    """Operator controls for Pi package bootstrap."""

    dry_run: bool = False
    json_output: bool = False
    project_local: bool = False
    yes: bool = False
    approve: bool = False
    timeout: float = 60.0


@dataclass(frozen=True)
class PiPackageState:
    """One catalog package's observable bootstrap state."""

    key: str
    spec: str
    status: str
    detail: str = ""


@dataclass(frozen=True)
class InstallReport:
    """Machine-readable installer outcome and its exact planned argv."""

    ready: bool
    status: str
    commands: tuple[tuple[str, ...], ...]
    detail: str = ""
    packages: tuple[PiPackageState, ...] = ()
    approval_persisted: bool = False


def _install_plan(
    pi_bin: Path,
    catalog: PiPackageCatalog,
    options: InstallOptions,
) -> tuple[tuple[str, ...], ...]:
    trust = "--approve" if options.approve else "--no-approve"
    scope = ("-l",) if options.project_local else ()
    commands: list[tuple[str, ...]] = [(str(pi_bin), "--version")]
    commands.extend(
        (str(pi_bin), "install", package.install_spec, *scope, trust)
        for package in catalog.packages
    )
    commands.append(
        (
            str(pi_bin),
            "--mode",
            "rpc",
            "--no-session",
            "--offline",
            "--no-context-files",
            "--extension",
            str(Path(__file__).with_name("pi_capability_probe.ts")),
            trust,
        )
    )
    return tuple(commands)


def _pi_child_env() -> dict[str, str]:
    allowed = (
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
    )
    env = {name: value for name in allowed if (value := os.environ.get(name))}
    env.setdefault("PATH", os.defpath)
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "PI_SKIP_VERSION_CHECK": "1",
            "PI_TELEMETRY": "0",
        }
    )
    return env


def install_pi_plugins(
    options: InstallOptions,
    *,
    catalog: PiPackageCatalog | None = None,
    pi_bin: Path | None = None,
    runner: CommandRunner = run_bounded_command,
) -> InstallReport:
    """Install the catalog package set or return a side-effect-free preview."""
    catalog = load_pi_package_catalog() if catalog is None else catalog
    if options.timeout <= 0:
        return InstallReport(False, "invalid_timeout", ())
    if options.approve and not options.project_local:
        return InstallReport(False, "approve_requires_project_local", ())
    resolved = pi_bin or Path(shutil.which("pi") or "pi")
    commands = _install_plan(resolved, catalog, options)
    states = tuple(
        PiPackageState(package.key, package.install_spec, "planned") for package in catalog.packages
    )
    if options.dry_run:
        return InstallReport(False, "dry_run", commands, packages=states)
    if not options.yes:
        return InstallReport(
            False,
            "confirmation_required",
            commands,
            "rerun with --yes",
            states,
        )
    identity = probe_pi_cli_identity(resolved, catalog, runner=runner, timeout=options.timeout)
    if not identity.ready:
        return InstallReport(False, identity.status, commands, identity.remediation, states)
    env = _pi_child_env()
    mutable_states = list(states)
    for index, command in enumerate(commands[1:-1]):
        result = runner(command, env=env, timeout=options.timeout)
        if result.timed_out:
            mutable_states[index] = PiPackageState(
                catalog.packages[index].key, command[2], "failed", "timeout"
            )
            return InstallReport(
                False, "install_timeout", commands, command[2], tuple(mutable_states)
            )
        if result.returncode != 0 or result.output_overflow:
            detail = (result.stderr or result.stdout or "package install failed")[:1000]
            mutable_states[index] = PiPackageState(
                catalog.packages[index].key, command[2], "failed", detail
            )
            return InstallReport(
                False,
                "install_failed",
                commands,
                f"{command[2]}: {detail}",
                tuple(mutable_states),
            )
        mutable_states[index] = PiPackageState(catalog.packages[index].key, command[2], "installed")
    if options.project_local and not options.approve:
        return InstallReport(
            False,
            "installed_unapproved",
            commands,
            "packages were retained; rerun with --project-local --yes --approve to verify once",
            tuple(mutable_states),
        )
    preflight = preflight_pi_environment(
        Path.cwd(),
        catalog=catalog,
        pi_bin=identity.executable,
        runner=runner,
        timeout=options.timeout,
        trust_override="--approve" if options.approve else "--no-approve",
    )
    return InstallReport(
        preflight.ready,
        preflight.status,
        commands,
        "" if preflight.ready else preflight.remediation_message(),
        tuple(
            PiPackageState(state.key, state.spec, "verified" if preflight.ready else state.status)
            for state in mutable_states
        ),
        approval_persisted=False,
    )


@dataclass(frozen=True)
class InventoryResult:
    """Statically verified package settings, scopes, and installed roots."""

    ready: bool
    status: str
    roots: dict[str, Path]
    scopes: dict[str, str]
    detail: str = ""


def _settings_packages(path: Path) -> tuple[str, ...]:
    try:
        document = _object(json.loads(path.read_text(encoding="utf-8")), str(path))
    except FileNotFoundError:
        return ()
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid Pi settings {path}: {exc}") from exc
    raw = document.get("packages", [])
    if not isinstance(raw, list):
        raise ValueError(f"{path}: packages must be an array")
    packages: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            source = entry
        elif isinstance(entry, dict):
            if entry.get("enabled", True) is not True or not isinstance(entry.get("source"), str):
                raise ValueError(f"{path}: package entry is disabled or malformed")
            source = cast(str, entry["source"])
        else:
            raise ValueError(f"{path}: package entry is malformed")
        if source in packages:
            raise ValueError(f"{path}: duplicate package source {source}")
        packages.append(source)
    return tuple(packages)


def _require_contained_root(base: Path, candidate: Path) -> Path:
    resolved_base = base.resolve(strict=True)
    resolved_candidate = candidate.resolve(strict=True)
    if resolved_candidate != resolved_base and resolved_base not in resolved_candidate.parents:
        raise ValueError(f"installed package root escapes {resolved_base}")
    return resolved_candidate


def _default_git_head(root: Path) -> str:
    result = run_bounded_command(("git", "-C", str(root), "rev-parse", "HEAD"), timeout=30)
    if result.returncode != 0 or result.timed_out or result.output_overflow:
        return ""
    return result.stdout.strip()


def inspect_pi_package_inventory(
    cwd: Path,
    catalog: PiPackageCatalog,
    *,
    pi_dir: Path | None = None,
    git_head: Callable[[Path], str] = _default_git_head,
    include_project: bool = True,
) -> InventoryResult:
    """Verify exact effective settings and installed roots without loading extensions."""
    user_root = (pi_dir or Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent"))).expanduser()
    project_root = cwd / ".pi"
    try:
        user_sources = _settings_packages(user_root / "settings.json")
        project_sources = (
            _settings_packages(project_root / "settings.json") if include_project else ()
        )
    except ValueError as exc:
        return InventoryResult(False, "package_settings_invalid", {}, {}, str(exc))
    roots: dict[str, Path] = {}
    scopes: dict[str, str] = {}
    for package in catalog.packages:
        matches_user = [
            source
            for source in user_sources
            if source.startswith(f"{package.kind}:{package.identity}@")
        ]
        matches_project = [
            source
            for source in project_sources
            if source.startswith(f"{package.kind}:{package.identity}@")
        ]
        matches = matches_project or matches_user
        if matches != [package.install_spec]:
            return InventoryResult(False, "package_inventory_mismatch", roots, scopes, package.key)
        scope = "project" if matches_project else "user"
        scope_root = project_root if matches_project else user_root
        candidate = (
            scope_root / "npm" / package.identity
            if package.kind == "npm"
            else scope_root / "git" / package.identity
        )
        try:
            root = _require_contained_root(scope_root, candidate)
            manifest = _object(
                json.loads((root / "package.json").read_text(encoding="utf-8")),
                f"{package.key} package.json",
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return InventoryResult(False, "package_root_invalid", roots, scopes, str(exc))
        if (
            manifest.get("name") != package.manifest_name
            or manifest.get("version") != package.manifest_version
        ):
            return InventoryResult(False, "package_identity_mismatch", roots, scopes, package.key)
        if package.kind == "git" and git_head(root) != package.pin:
            return InventoryResult(False, "package_identity_mismatch", roots, scopes, package.key)
        roots[package.key] = root
        scopes[package.key] = scope
    return InventoryResult(True, "ready", roots, scopes)


@dataclass(frozen=True)
class CapabilityResult:
    """Machine-readable command/tool capability verification result."""

    ready: bool
    status: str
    detail: str = ""


def _source_matches(entry: dict[str, Any], root: Path, scope: str) -> bool:
    info = entry.get("sourceInfo")
    if not isinstance(info, dict):
        return False
    raw_base_dir = info.get("baseDir")
    raw_source_path = info.get("path")
    if not isinstance(raw_base_dir, str) or not isinstance(raw_source_path, str):
        return False
    try:
        base_dir = Path(raw_base_dir).resolve()
        source_path = Path(raw_source_path).resolve()
    except OSError:
        return False
    resolved_root = root.resolve()
    return (
        info.get("origin") == "package"
        and info.get("scope") == scope
        and base_dir == resolved_root
        and (source_path == resolved_root or resolved_root in source_path.parents)
    )


def verify_capability_inventory(
    payload: dict[str, Any],
    inventory: InventoryResult,
    catalog: PiPackageCatalog,
) -> CapabilityResult:
    """Require every command and active tool to originate in its verified package root."""
    if not inventory.ready:
        return CapabilityResult(False, "package_inventory_mismatch", inventory.detail)
    commands = payload.get("commands")
    reported_commands = payload.get("reported_commands")
    active_tools = payload.get("active_tools")
    all_tools = payload.get("all_tools")
    if (
        not isinstance(commands, list)
        or not isinstance(reported_commands, list)
        or not isinstance(active_tools, list)
        or not isinstance(all_tools, list)
    ):
        return CapabilityResult(False, "capability_payload_malformed")
    command_entries = cast(list[dict[str, Any]], commands)
    reported_command_entries = cast(list[dict[str, Any]], reported_commands)
    if not all(isinstance(name, str) for name in active_tools):
        return CapabilityResult(False, "capability_payload_malformed")
    active_names = cast(list[str], active_tools)
    all_entries = cast(list[dict[str, Any]], all_tools)
    for package in catalog.packages:
        root = inventory.roots[package.key]
        scope = inventory.scopes[package.key]
        for name in package.commands:
            candidates = [entry for entry in command_entries if entry.get("name") == name]
            reported = [entry for entry in reported_command_entries if entry.get("name") == name]
            if (
                len(candidates) != 1
                or len(reported) != 1
                or not _source_matches(candidates[0], root, scope)
                or not _source_matches(reported[0], root, scope)
            ):
                return CapabilityResult(False, "capability_provenance_mismatch", name)
        for name in package.tools:
            known = [entry for entry in all_entries if entry.get("name") == name]
            if (
                active_names.count(name) != 1
                or len(known) != 1
                or not _source_matches(known[0], root, scope)
            ):
                return CapabilityResult(False, "capability_provenance_mismatch", name)
    return CapabilityResult(True, "ready")


@dataclass(frozen=True)
class PiPreflightResult:
    """Complete static-inventory and dynamic-capability admission result."""

    ready: bool
    status: str
    remediation: str
    detail: str = ""
    inventory: InventoryResult | None = None

    @classmethod
    def ready_result(cls, inventory: InventoryResult | None = None) -> PiPreflightResult:
        """Construct the successful package-preflight result."""
        return cls(True, "ready", "", inventory=inventory)

    def remediation_message(self) -> str:
        """Return an actionable operator-facing failure description."""
        if self.ready:
            return "Pi package preflight is ready"
        detail = f" ({self.detail})" if self.detail else ""
        return f"Pi package preflight failed: {self.status}{detail}. {self.remediation}".strip()


def _preflight_failure(
    status: str,
    catalog: PiPackageCatalog,
    *,
    detail: str = "",
    inventory: InventoryResult | None = None,
) -> PiPreflightResult:
    remediation = "Run hephaestus-install-pi-plugins --global --yes --no-approve"
    if status.startswith("pi_cli_"):
        remediation = _pi_remediation(catalog)
    return PiPreflightResult(False, status, remediation, detail, inventory)


def _rpc_events(stdout: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        try:
            events.append(_object(json.loads(line), "Pi RPC event"))
        except (json.JSONDecodeError, ValueError) as exc:
            raise ValueError("Pi RPC output contains malformed JSONL") from exc
    return events


def _nonce_notification(event: dict[str, Any], nonce: str) -> dict[str, Any] | None:
    if event.get("type") != "extension_ui_request" or event.get("method") != "notify":
        return None
    message = event.get("message")
    if not isinstance(message, str):
        return None
    try:
        payload = _object(json.loads(message), "preflight notification")
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if payload.get("nonce") == nonce else None


def _parse_capability_rpc(stdout: str, nonce: str) -> dict[str, Any]:
    events = _rpc_events(stdout)
    command_responses: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "response" and event.get("id") == "hephaestus-commands":
            command_responses.append(event)
    if len(command_responses) != 1 or command_responses[0].get("success") is not True:
        raise ValueError("Pi RPC output lacks one correlated get_commands response")
    data = _object(command_responses[0].get("data"), "get_commands response")
    commands = data.get("commands")
    if not isinstance(commands, list):
        raise ValueError("get_commands response lacks commands")
    matched = [payload for event in events if (payload := _nonce_notification(event, nonce))]
    if len(matched) != 1:
        raise ValueError("Pi RPC output lacks one nonce-bound preflight notification")
    payload = dict(matched[0])
    payload["commands"] = commands
    return payload


def preflight_pi_environment(
    cwd: Path,
    *,
    catalog: PiPackageCatalog | None = None,
    pi_bin: Path | None = None,
    pi_dir: Path | None = None,
    runner: CommandRunner = run_bounded_command,
    git_head: Callable[[Path], str] = _default_git_head,
    timeout: float = 30.0,
    trust_override: str | None = None,
) -> PiPreflightResult:
    """Verify CLI, settings, package roots, and provenance-bound capabilities in order."""
    catalog = load_pi_package_catalog() if catalog is None else catalog
    discovered = pi_bin or (Path(found) if (found := shutil.which("pi")) else Path("pi"))
    identity = probe_pi_cli_identity(discovered, catalog, runner=runner, timeout=timeout)
    if not identity.ready or identity.executable is None:
        return _preflight_failure(identity.status, catalog, detail=identity.detail)
    inventory = inspect_pi_package_inventory(
        cwd,
        catalog,
        pi_dir=pi_dir,
        git_head=git_head,
        include_project=trust_override != "--no-approve",
    )
    if not inventory.ready:
        return _preflight_failure(
            inventory.status, catalog, detail=inventory.detail, inventory=inventory
        )
    nonce = secrets.token_hex(16)
    command: tuple[str, ...] = (
        str(identity.executable),
        "--mode",
        "rpc",
        "--no-session",
        "--offline",
        "--no-context-files",
        "--extension",
        str(Path(__file__).with_name("pi_capability_probe.ts")),
    )
    if trust_override is not None:
        command += (trust_override,)
    requests = "\n".join(
        (
            json.dumps({"type": "get_commands", "id": "hephaestus-commands"}),
            json.dumps(
                {
                    "type": "prompt",
                    "id": "hephaestus-probe",
                    "message": f"/hephaestus-preflight {nonce}",
                }
            ),
            "",
        )
    )
    try:
        result = runner(
            command,
            cwd=cwd,
            env=_pi_child_env(),
            timeout=timeout,
            input_text=requests,
            keep_stdin_open=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _preflight_failure(
            "capability_probe_failed", catalog, detail=str(exc), inventory=inventory
        )
    if result.timed_out:
        return _preflight_failure("capability_probe_timeout", catalog, inventory=inventory)
    if result.returncode != 0 or result.output_overflow:
        return _preflight_failure(
            "capability_probe_failed",
            catalog,
            detail=(result.stderr or result.stdout)[:1000],
            inventory=inventory,
        )
    try:
        payload = _parse_capability_rpc(result.stdout, nonce)
    except ValueError as exc:
        return _preflight_failure(
            "capability_payload_malformed", catalog, detail=str(exc), inventory=inventory
        )
    capabilities = verify_capability_inventory(payload, inventory, catalog)
    if not capabilities.ready:
        return _preflight_failure(
            capabilities.status,
            catalog,
            detail=capabilities.detail,
            inventory=inventory,
        )
    return PiPreflightResult.ready_result(inventory)


def build_parser() -> argparse.ArgumentParser:
    """Build the ``hephaestus-install-pi-plugins`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="hephaestus-install-pi-plugins",
        description="Install and preflight the catalog-pinned Pi package set.",
    )
    add_version_arg(parser)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="project_local", action="store_false")
    scope.add_argument("--project-local", action="store_true")
    parser.set_defaults(project_local=False)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", dest="json_output", action="store_true")
    parser.add_argument("--yes", action="store_true")
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument("--approve", action="store_true")
    approval.add_argument("--no-approve", dest="approve", action="store_false")
    parser.set_defaults(approve=False)
    parser.add_argument("--timeout", type=float, default=60.0, metavar="SECONDS")
    return parser


def _report_document(report: InstallReport) -> dict[str, Any]:
    return {
        "ready": report.ready,
        "status": report.status,
        "commands": [list(command) for command in report.commands],
        "detail": report.detail,
        "packages": [
            {
                "key": package.key,
                "spec": package.spec,
                "status": package.status,
                "detail": package.detail,
            }
            for package in report.packages
        ],
        "approval_persisted": report.approval_persisted,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the package bootstrap CLI and return its stable process status."""
    args = build_parser().parse_args(argv)
    yes = args.yes
    if not args.dry_run and not yes and sys.stdin.isatty() and not args.json_output:
        answer = input("Install the catalog-pinned Pi packages? [y/N] ").strip().lower()
        yes = answer in {"y", "yes"}
    options = InstallOptions(
        dry_run=args.dry_run,
        json_output=args.json_output,
        project_local=args.project_local,
        yes=yes,
        approve=args.approve,
        timeout=args.timeout,
    )
    report = install_pi_plugins(options)
    if args.json_output:
        print(json.dumps(_report_document(report), sort_keys=True))
    else:
        print(f"Pi package bootstrap: {report.status}")
        if report.detail:
            print(report.detail, file=sys.stderr)
        if report.status == "dry_run":
            for command in report.commands:
                print("  " + " ".join(command))
    if report.ready or report.status == "dry_run":
        return 0
    if report.status in {
        "approve_requires_project_local",
        "confirmation_required",
        "invalid_timeout",
        "installed_unapproved",
    }:
        return 2
    return 1
