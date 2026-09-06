"""Opt-in end-to-end proof for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def _git(cwd: Path, *argv: str) -> str:
    """Run one fixture Git command."""
    result = subprocess.run(("git", *argv), cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


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
    image = (
        Path(image_text)
        if image_text
        else Path.cwd() / "build/host-verification/hephaestus-ci.sqsh"
    )
    expected_sha256 = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_SHA256", "")
    authority_text = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_AUTHORITY", "")
    quota_root_text = os.environ.get("HEPHAESTUS_HOST_VERIFICATION_PYXIS_QUOTA_ROOT", "")
    if missing:
        pytest.fail("live Pyxis prerequisites unavailable: " + ", ".join(missing))
    if not image.is_file() or image.is_symlink():
        pytest.fail(f"prepared Pyxis squashfs image is unavailable: {image}")
    if not expected_sha256 or not authority_text or not quota_root_text:
        pytest.fail("live Pyxis authority digest, provenance, and quota root are required")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch", "main")
    _git(checkout, "config", "user.name", "Pyxis integration")
    _git(checkout, "config", "user.email", "pyxis@example.invalid")
    (checkout / "tracked.txt").write_text("immutable\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-m", "fixture")
    head = _git(checkout, "rev-parse", "HEAD")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    program = (
        "from pathlib import Path; import socket, subprocess; "
        f"\ntry: socket.create_connection(('127.0.0.1', {port}), timeout=1); "
        "raise SystemExit('network access was not denied')\nexcept OSError: pass; "
        "assert Path('.git').is_file(); "
        "assert Path('build').is_symlink(); "
        "assert Path('pi-smoke-logs').is_dir(); "
        "try: Path('tracked.txt').write_text('changed')\nexcept OSError: pass\n"
        "else: raise SystemExit('source was writable')\n"
        "assert subprocess.run(('git', 'config', '--local', 'pyxis.probe', '1')).returncode != 0; "
        "Path('build/probe.txt').write_text('scratch'); "
        "Path('coverage.xml').write_text('coverage'); "
        "Path('pi-smoke-logs/probe.txt').write_text('logs')"
    )
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_verification_pyxis_image=image,
        host_verification_pyxis_sha256=expected_sha256,
        host_verification_pyxis_authority=Path(authority_text),
        host_verification_pyxis_quota_root=Path(quota_root_text),
    )
    try:
        result = pool._run_build_test(
            BuildTestJob(
                repo="fixture/repo",
                cwd=checkout,
                argv=("uv", "run", "python", "-c", program),
                timeout_s=300,
                expected_head_sha=head,
                immutable_source=True,
            )
        )
    finally:
        listener.close()
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
