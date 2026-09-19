"""Select a local container runtime for disposable PostgreSQL tests."""

from __future__ import annotations

import os
import subprocess
import sys


def runtime_argv() -> tuple[str, ...]:
    """Return fixed arguments after local runtime checks."""
    for name in (
        "CONTAINER_HOST",
        "CONTAINER_CONNECTION",
        "CONTAINER_SSHKEY",
        "DOCKER_HOST",
        "DOCKER_CONTEXT",
        "DOCKER_TLS_VERIFY",
        "DOCKER_CERT_PATH",
    ):
        if os.environ.get(name):
            raise ValueError("Remote container selection is not permitted.")
    in_ci = os.environ.get("GITHUB_ACTIONS") == "true"
    runtime = os.environ.get("COMET_TEST_CONTAINER_RUNTIME", "docker" if in_ci else "podman")
    if runtime == "docker" and in_ci:
        return ("docker", "--host", "unix:///var/run/docker.sock")
    if runtime != "podman" or os.geteuid() == 0:
        raise ValueError("A local rootless container runtime is required.")
    prefix = ("podman", "--remote=false")
    result = subprocess.run(
        (*prefix, "info", "--format", "{{.Host.Security.Rootless}}"),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise ValueError("A local rootless container runtime is required.")
    return prefix


def main() -> int:
    """Write only fixed arguments or a safe failure message."""
    try:
        arguments = runtime_argv()
    except (ValueError, OSError, subprocess.SubprocessError):
        print("The PostgreSQL test container runtime is unavailable or invalid.", file=sys.stderr)
        return 2
    print("\n".join(arguments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
