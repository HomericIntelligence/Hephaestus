"""Opt-in end-to-end proof for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import json
import os
import queue
import secrets
import shutil
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.host_verification_pyxis import (
    DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE,
    PyxisExecutionPlacement,
)
from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def _git(cwd: Path, *argv: str) -> str:
    """Run one fixture Git command."""
    result = subprocess.run(("git", *argv), cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _validate_network_control(
    result: subprocess.CompletedProcess[str], boot_id: str, hostname: str, token: str
) -> None:
    """Require the exact host boot and a successful listener challenge."""
    assert result.returncode == 0, "network control command failed"
    assert len(result.stdout) <= 4096, "network control output is oversized"
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise AssertionError("network control output is invalid") from exc
    assert value == {"boot_id": boot_id, "hostname": hostname, "token": token}, (
        "network control did not prove the same host and listener"
    )


def _run_controlled_job(
    pool: WorkerPool, job: BuildTestJob, control: Callable[[], None]
) -> JobResult:
    """Require positive controls on both sides of the worker execution."""
    control()
    try:
        return pool._run_build_test(job)
    finally:
        control()


@contextmanager
def _control_listener() -> Iterator[tuple[int, str]]:
    """Keep one local challenge listener alive for both positive controls."""
    token = secrets.token_hex(32)
    stopped = threading.Event()
    failures: queue.Queue[Exception] = queue.Queue()
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(8)
        listener.settimeout(0.2)

        def serve() -> None:
            try:
                while not stopped.is_set():
                    try:
                        connection, _ = listener.accept()
                    except TimeoutError:
                        continue
                    with connection:
                        connection.settimeout(2)
                        connection.sendall(token.encode("ascii"))
            except Exception as exc:
                failures.put(exc)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1], token
        finally:
            stopped.set()
            thread.join(timeout=3)
            assert not thread.is_alive(), "network control listener did not stop"
            assert failures.empty(), "network control listener failed"


def _same_node_network_control(
    executable: str,
    placement: PyxisExecutionPlacement,
    port: int,
    token: str,
    boot_id: str,
    hostname: str,
) -> None:
    """Run a benign positive control in the selected allocation and node."""
    program = """
import json
import socket
import sys
from pathlib import Path

with socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=2) as connection:
    connection.settimeout(2)
    chunks = []
    while sum(map(len, chunks)) < 64:
        chunk = connection.recv(64 - sum(map(len, chunks)))
        if not chunk:
            break
        chunks.append(chunk)
print(json.dumps({
    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
    'hostname': socket.gethostname(),
    'token': b''.join(chunks).decode('ascii'),
}))
"""
    result = subprocess.run(
        (
            executable,
            f"--jobid={placement.allocation_id}",
            f"--nodelist={placement.node}",
            "--exclusive",
            "--nodes=1",
            "--ntasks=1",
            "--cpus-per-task=2",
            "--mem=4096M",
            "--time=00:00:30",
            "--kill-on-bad-exit=1",
            "--export=NONE",
            "/usr/bin/env",
            "-i",
            sys.executable,
            "-I",
            "-c",
            program,
            str(port),
        ),
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    _validate_network_control(result, boot_id, hostname, token)


@pytest.mark.integration
@pytest.mark.pyxis
def test_linux_pyxis_host_verification_boundary(
    tmp_path: Path, require_pyxis_host_verification: bool
) -> None:
    """Run the real Pyxis boundary only after explicit operator opt-in."""
    if not require_pyxis_host_verification:
        pytest.skip("pass --require-pyxis-host-verification for live Pyxis evidence")
    if sys.platform != "linux":
        pytest.fail("--require-pyxis-host-verification requires a Linux host")

    missing = [name for name in ("srun", "enroot") if shutil.which(name) is None]
    image_text = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_IMAGE", "")
    image = Path(image_text) if image_text else DEFAULT_HOST_VERIFICATION_PYXIS_IMAGE
    expected_sha256 = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_SHA256", "")
    authority_text = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_AUTHORITY", "")
    quota_root_text = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_QUOTA_ROOT", "")
    if missing:
        pytest.fail("live Pyxis prerequisites unavailable: " + ", ".join(missing))
    if not image.is_file() or image.is_symlink():
        pytest.fail(f"prepared Pyxis squashfs image is unavailable: {image}")
    if not expected_sha256 or not authority_text or not quota_root_text:
        pytest.fail("live Pyxis authority digest, provenance, and quota root are required")

    try:
        placement = PyxisExecutionPlacement(
            allocation_id=os.environ.get("SLURM_JOB_ID", ""),
            node=os.environ.get("SLURMD_NODENAME", ""),
        )
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        assert boot_id, "host boot identity is missing"
    except (OSError, ValueError, AssertionError) as exc:
        pytest.fail(f"live Pyxis acceptance requires an allocated-node batch process: {exc}")
    hostname = socket.gethostname()
    host_namespaces = {name: os.readlink(f"/proc/self/ns/{name}") for name in ("net", "ipc", "uts")}
    executable = shutil.which("srun", path="/usr/local/bin:/usr/bin:/bin")
    if executable is None:
        pytest.fail("trusted srun is unavailable for the placement control")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch", "main")
    _git(checkout, "config", "user.name", "Pyxis integration")
    _git(checkout, "config", "user.email", "pyxis@example.invalid")
    (checkout / "tracked.txt").write_text("immutable\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-m", "fixture")
    head = _git(checkout, "rev-parse", "HEAD")

    with _control_listener() as (port, token):
        program = f"""
from pathlib import Path
import os
import socket
import subprocess

for name, host_namespace in {host_namespaces!r}.items():
    assert os.readlink('/proc/self/ns/' + name) != host_namespace
status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines())
assert status['NoNewPrivs'].strip() == '1'
for key in ('CapInh', 'CapPrm', 'CapEff', 'CapBnd', 'CapAmb'):
    assert int(status[key].strip(), 16) == 0

try:
    socket.create_connection(('127.0.0.1', {port}), timeout=1)
except OSError:
    pass
else:
    raise SystemExit('network access was not denied')

assert Path('.git').is_file()
assert Path('build').is_symlink()
assert Path('pi-smoke-logs').is_dir()
try:
    Path('tracked.txt').write_text('changed')
except OSError:
    pass
else:
    raise SystemExit('source was writable')

assert subprocess.run(('git', 'config', '--local', 'pyxis.probe', '1')).returncode != 0
Path('build/probe.txt').write_text('scratch')
Path('coverage.xml').write_text('coverage')
Path('pi-smoke-logs/probe.txt').write_text('logs')
"""
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            lock_dir=tmp_path / "locks",
            host_verification_pyxis_image=image,
            host_verification_pyxis_sha256=expected_sha256,
            host_verification_pyxis_authority=Path(authority_text),
            host_verification_pyxis_quota_root=Path(quota_root_text),
            host_verification_pyxis_placement=placement,
        )
        try:
            result = _run_controlled_job(
                pool,
                BuildTestJob(
                    repo="fixture/repo",
                    cwd=checkout,
                    argv=("uv", "run", "python", "-c", program),
                    timeout_s=300,
                    expected_head_sha=head,
                    immutable_source=True,
                ),
                lambda: _same_node_network_control(
                    executable, placement, port, token, boot_id, hostname
                ),
            )
        finally:
            pool.shutdown(mark_interrupted=False)

        assert result.ok is True, (
            f"Pyxis host verification failed: {result.error}\n{result.stderr_tail}"
        )
        assert result.value == {
            "container_image": result.value["container_image"],
            "container_image_sha256": result.value["container_image_sha256"],
            "container_image_id": result.value["container_image_id"],
            "container_image_reference": result.value["container_image_reference"],
            "containerfile_sha256": result.value["containerfile_sha256"],
            "container_source_revision": result.value["container_source_revision"],
            "container_runtime": "pyxis",
            "failure_kind": "none",
            "head_sha": head,
            "immutable_source": True,
            "platform": "linux",
            "status": "passed",
        }
