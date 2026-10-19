"""Real-broker fixtures for the durable-failure integration tests (#2395).

The ``nats_server`` session fixture starts a temporary file-backed
``nats-server`` with JetStream enabled. The binary comes from
``HEPHAESTUS_NATS_SERVER_BIN`` or ``build/tools/nats-server``; all test
state lives beneath ``build/test-nats/``. The broker ``max_payload`` sits
above the 1,414,488-byte DLQ bound so full-size envelopes publish.

Tests skip by default when no binary is available; set
``HEPHAESTUS_REQUIRE_NATS_SERVER=1`` to fail instead.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

_SERVER_BIN_ENV = "HEPHAESTUS_NATS_SERVER_BIN"
_REQUIRE_ENV = "HEPHAESTUS_REQUIRE_NATS_SERVER"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_STORE_ROOT = _REPO_ROOT / "build" / "test-nats"

# 2 MiB: comfortably above the 1,414,488-byte DLQ envelope bound so the
# full-size payload retention test publishes without broker rejection.
_MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
_STARTUP_TIMEOUT_SECONDS = 15.0


@dataclass
class NatsServerHandle:
    """One running temporary nats-server process.

    Attributes:
        url: Client URL of the broker (``nats://127.0.0.1:<port>``).
        store_dir: JetStream file-store directory for this server.
        process: The server process.

    """

    url: str
    store_dir: Path
    process: subprocess.Popen[bytes]


def _server_bin() -> str | None:
    raw = os.environ.get(_SERVER_BIN_ENV)
    if raw:
        return raw
    candidate = _REPO_ROOT / "build" / "tools" / "nats-server"
    return str(candidate) if candidate.is_file() else None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_for_ready(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"nats-server exited early with code {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    process.kill()
    raise RuntimeError(f"nats-server did not start within {_STARTUP_TIMEOUT_SECONDS}s")


@pytest.fixture(scope="session")
def nats_server() -> Iterator[NatsServerHandle]:
    """Start one isolated file-backed nats-server for the test session."""
    binary = _server_bin()
    if binary is None:
        if os.environ.get(_REQUIRE_ENV) == "1":
            pytest.fail(
                f"{_REQUIRE_ENV}=1 but no nats-server binary; set {_SERVER_BIN_ENV} "
                "or install one into build/tools/"
            )
        pytest.skip(f"nats-server binary unavailable; set {_SERVER_BIN_ENV}")

    port = _free_port()
    _STORE_ROOT.mkdir(parents=True, exist_ok=True)
    store_dir = Path(tempfile.mkdtemp(prefix="store-", dir=_STORE_ROOT))
    config_path = store_dir / "nats-server.conf"
    config_path.write_text(
        f"port: {port}\n"
        f"server_name: hephaestus-test-{port}\n"
        "jetstream {\n"
        f"  store_dir: {store_dir / 'jetstream'}\n"
        # The provisioned failure resources declare max_bytes reservations of
        # about 1.66 GiB in total; the broker rejects stream creation when
        # declared reservations exceed the store limit, so cap above them.
        "  max_mem_store: 128MB\n"
        "  max_file_store: 4GB\n"
        "}\n"
        f"max_payload: {_MAX_PAYLOAD_BYTES}\n"
    )
    log_file = (_STORE_ROOT / f"server-{port}.log").open("w")
    process = subprocess.Popen(
        [binary, "-c", str(config_path)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_ready(port, process)
    except Exception:
        shutil.rmtree(store_dir, ignore_errors=True)
        raise

    handle = NatsServerHandle(
        url=f"nats://127.0.0.1:{port}",
        store_dir=store_dir,
        process=process,
    )
    yield handle

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    log_file.close()
    shutil.rmtree(store_dir, ignore_errors=True)


def unique_name(base: str) -> str:
    """Return a per-process unique stream/consumer name for test isolation."""
    return f"{base}_{os.getpid()}_{time.monotonic_ns() % 1_000_000_000}"
