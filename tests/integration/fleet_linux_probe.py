"""Measure the pinned Linux process boundary with generated files and no authentication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from hephaestus.automation.fleet_isolation import shell_environment_policy, validate_worker_storage
from hephaestus.automation.fleet_provider import CodexAppServer


class ProbeError(ValueError):
    """Retain bounded diagnostics from fixed no-auth probe commands."""

    def __init__(self, code: str, result: dict) -> None:
        """Retain exit status and bounded sandbox-helper diagnostics."""
        super().__init__(code)
        self.details = {
            "commandExitCode": result.get("exitCode"),
            "commandStderr": str(result.get("stderr", ""))[:2048],
        }


def process_inventory(nonce: str) -> list[tuple[int, str]]:
    """Locate only the probe child, with its kernel process-start identity."""
    result = []
    for path in Path("/proc").iterdir():
        if not path.name.isdecimal():
            continue
        try:
            arguments = (path / "cmdline").read_bytes()
            if nonce.encode() not in arguments.split(b"\0"):
                continue
            start = (path / "stat").read_text().rsplit(") ", 1)[1].split()[19]
            result.append((int(path.name), start))
        except (FileNotFoundError, ProcessLookupError):
            continue
    return result


def cleanup_probe_children(nonce: str) -> None:
    """Stop surviving synthetic children without signaling unrelated processes."""
    for pid, start in process_inventory(nonce):
        try:
            current = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            if current == start:
                if os.getpgid(pid) == pid:
                    os.killpg(pid, signal.SIGKILL)
                else:
                    os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue


def write_probe_config(home: Path, workspace: Path, state: Path) -> dict[str, object]:
    """Use the same filesystem and shell environment settings as the Fleet worker."""
    policy = shell_environment_policy(workspace)
    environment = policy["set"]
    if not isinstance(environment, dict):
        raise ValueError("invalid_shell_environment")
    filesystem = {
        ":minimal": "read",
        str(workspace): "write",
        str(home): "deny",
        str(state): "deny",
    }
    lines = ['default_permissions = "fleet"', 'cli_auth_credentials_store = "file"']
    sections: tuple[tuple[str, dict[str, Any]], ...] = (
        ("permissions.fleet.filesystem", filesystem),
        ("permissions.fleet.network", {"enabled": False}),
        ("features", {"shell_snapshot": False, "multi_agent": False, "multi_agent_v2": False}),
        ("agents", {"enabled": False}),
        ("shell_environment_policy", {key: value for key, value in policy.items() if key != "set"}),
        ("shell_environment_policy.set", environment),
    )
    for section, values in sections:
        lines.append(f"[{section}]")
        lines.extend(f"{json.dumps(key)} = {json.dumps(value)}" for key, value in values.items())
    (home / "config.toml").write_text("\n".join(lines) + "\n")
    return policy


def command(provider: CodexAppServer, workspace: Path, script: str, *arguments: str) -> dict:
    """Run one fixed no-auth command with a short deadline and bounded output."""
    return provider.request(
        "command/exec",
        {
            "cwd": str(workspace),
            "permissionProfile": "fleet",
            "timeoutMs": 5000,
            "outputBytesCap": 8192,
            "command": ["/bin/sh", "-c", script, "fleet-probe", *arguments],
        },
        timeout=7,
    )


def probe(root: Path, executable: Path) -> dict:
    """Return observations; this report cannot authorize Fleet admission."""
    if sys.platform != "linux":
        raise ValueError("linux_probe_requires_linux")
    if not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError("probe_root_must_be_private")
    executable = executable.resolve(strict=True)
    nonce = "fleet-probe-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="no-auth-", dir=root) as temporary:
        work = Path(temporary)
        home, state, workspace, sibling = [
            work / name for name in ("auth", "state", "work", "peer")
        ]
        for path in (home, state, workspace, sibling):
            path.mkdir(mode=0o700)
        validate_worker_storage(home, state, workspace)
        policy = write_probe_config(home, workspace, state)
        provider = CodexAppServer([str(executable)], home, timeout=10)
        observed = {}
        try:
            provider.start()
            with tempfile.TemporaryDirectory(prefix="fleet-peer-", dir="/tmp") as scratch:
                arguments: list[str] = []
                for name, directory in (
                    ("workspace", workspace),
                    ("sibling", sibling),
                    ("authority", home),
                    ("state", state),
                    ("sharedScratch", Path(scratch)),
                ):
                    marker = directory / "synthetic-marker"
                    marker.write_text("synthetic marker only\n")
                    arguments.extend((name, str(marker)))
                environment = policy["set"]
                if not isinstance(environment, dict) or not isinstance(
                    environment.get("HOME"), str
                ):
                    raise ValueError("invalid_shell_environment")
                result = command(provider, workspace, _FILES, environment["HOME"], *arguments)
                if result.get("exitCode") != 0:
                    raise ProbeError("synthetic_file_probe_failed", result)
                observed.update(
                    {
                        key: value == "1"
                        for key, value in (
                            line.split("=", 1) for line in result["stdout"].splitlines()
                        )
                    }
                )
            ready = workspace / "child-ready"
            detached = command(provider, workspace, _DETACHED, nonce, str(ready))
            observed["detachedStarted"] = detached.get("exitCode") == 0 and ready.exists()
            observed["detachedAfterCommand"] = len(process_inventory(nonce))
            observed["providerGroupStopped"] = provider.close()
            first = process_inventory(nonce)
            time.sleep(0.05)
            second = process_inventory(nonce)
            observed["detachedAfterProviderClose"] = len(second)
            observed["detachedCleanupConfirmed"] = not first and not second
            observed["authFileAbsent"] = not (home / "auth.json").exists()
        finally:
            try:
                provider.close()
            finally:
                cleanup_probe_children(nonce)
        required = {
            "privateHome": True,
            "providerHomeAbsent": True,
            "workspaceRead": True,
            "workspaceWrite": True,
            **{
                name + access: False
                for name in ("sibling", "authority", "state", "sharedScratch")
                for access in ("Read", "Write")
            },
            "detachedStarted": True,
            "providerGroupStopped": True,
            "detachedCleanupConfirmed": True,
            "authFileAbsent": True,
        }
        failures = [key for key, value in required.items() if observed.get(key) != value]
        return {
            "schema": "hi/fleet/linux-probe/v1",
            "providerVersion": "0.153.4",
            "executableSha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
            "observations": observed,
            "passed": not failures,
            "failures": failures,
            "authorizesAdmission": False,
        }


_FILES = r"""
if test "$HOME" = "$1"; then echo privateHome=1; else echo privateHome=0; fi
if test -z "${CODEX_HOME+x}"; then echo providerHomeAbsent=1; else echo providerHomeAbsent=0; fi
shift
while test "$#" -gt 0; do
    name=$1; marker=$2; shift 2
    if /bin/cat "$marker" >/dev/null 2>&1; then echo "${name}Read=1"; else echo "${name}Read=0"; fi
    if (printf synthetic > "$marker") 2>/dev/null; then
        echo "${name}Write=1"
    else
        echo "${name}Write=0"
    fi
done
"""

_DETACHED = r"""
setsid /bin/sh -c 'printf ready > "$1"; sleep 30; : "$0"' "$1" "$2" >/dev/null 2>&1 &
count=0
while ! test -f "$2"; do
    count=$((count+1))
    if test "$count" -gt 100; then exit 2; fi
    sleep 0.01
done
"""


def main() -> int:
    """Print the measured result or a bounded diagnostic error."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--codex-bin", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = probe(args.root, args.codex_bin)
    except (OSError, RuntimeError, ValueError) as error:
        result = {
            "schema": "hi/fleet/linux-probe/v1",
            "passed": False,
            "error": type(error).__name__,
        }
        if isinstance(error, ValueError):
            result["errorCode"] = str(error)[:200]
        if isinstance(error, ProbeError):
            result.update(error.details)
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
