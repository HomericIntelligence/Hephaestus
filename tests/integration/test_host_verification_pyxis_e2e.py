"""Opt-in end-to-end proof for the Linux Pyxis host-verification boundary."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from hephaestus.automation.pipeline.host_verification_pyxis import (
    PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES,
    build_pyxis_environment,
    build_pyxis_srun_command,
    resolve_trusted_srun_executable,
    validate_pyxis_image,
)
from hephaestus.automation.pipeline.jobs import BuildTestJob
from hephaestus.automation.pipeline.worker_pool import (
    _HOST_VERIFICATION_CPU_MAX_S,
    _HOST_VERIFICATION_OPEN_FILES_MAX,
    _HOST_VERIFICATION_OUTPUT_FILE_MAX_BLOCKS,
    _HOST_VERIFICATION_PROCESS_HEADROOM,
    WorkerPool,
    _linux_resource_limited_command,
)


def _git(cwd: Path, *argv: str) -> str:
    """Run one fixture Git command."""
    result = subprocess.run(("git", *argv), cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _boundary_probe_program(
    *, expected_head: str, expected_job_id: str, expected_tracked: str, timeout_s: int
) -> str:
    """Return the executable source for the nested Pyxis boundary probe."""
    cpu_limit = min(timeout_s, _HOST_VERIFICATION_CPU_MAX_S)
    file_limit = _HOST_VERIFICATION_OUTPUT_FILE_MAX_BLOCKS * 512
    return "\n".join(
        (
            "from pathlib import Path",
            "import json",
            "import os",
            "import resource",
            "import socket",
            "import subprocess",
            "assert os.geteuid() != 0",
            "parent_namespaces = json.loads(os.environ['HEPHAESTUS_PYXIS_PARENT_NAMESPACES'])",
            "assert set(parent_namespaces) == {'user', 'net', 'ipc', 'uts'}",
            "assert all(os.readlink(f'/proc/self/ns/{name}') != parent_namespaces[name] "
            "for name in ('user', 'net', 'ipc', 'uts')), "
            "'nested namespaces were not created'",
            "assert os.environ['HEPHAESTUS_PYXIS_PARENT_NODE'] == socket.gethostname()",
            "assert [name for _, name in socket.if_nameindex()] == ['lo']",
            "try:",
            "    socket.create_connection((",
            "        '127.0.0.1', int(os.environ['HEPHAESTUS_PYXIS_PARENT_PORT'])",
            "    ), timeout=1)",
            "except OSError:",
            "    pass",
            "else:",
            "    raise SystemExit('network access was not denied')",
            "slurm = json.loads(os.environ['HEPHAESTUS_PYXIS_SLURM_EVIDENCE'])",
            f"assert slurm['job_id'] == {expected_job_id!r}",
            "assert slurm['step_id'].isdecimal()",
            "assert slurm['step_nodelist'] == socket.gethostname()",
            "assert slurm['step_nodes'] == '1'",
            "assert slurm['cpus_per_task'] == '2'",
            "assert 0 < int(slurm['job_start_time']) < int(slurm['job_end_time'])",
            "assert 1 <= len(os.sched_getaffinity(0)) <= 2",
            "limits = {",
            "    'CPU': resource.getrlimit(resource.RLIMIT_CPU),",
            "    'FSIZE': resource.getrlimit(resource.RLIMIT_FSIZE),",
            "    'NPROC': resource.getrlimit(resource.RLIMIT_NPROC),",
            "    'NOFILE': resource.getrlimit(resource.RLIMIT_NOFILE),",
            "}",
            f"assert limits['CPU'][0] == {cpu_limit}, limits",
            f"assert limits['FSIZE'][0] == {file_limit}, limits",
            f"assert limits['NPROC'][0] == {_HOST_VERIFICATION_PROCESS_HEADROOM}, limits",
            f"assert limits['NOFILE'][0] == {_HOST_VERIFICATION_OPEN_FILES_MAX}, limits",
            "cgroup = next(line.split('::', 1)[1] for line in "
            "Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))",
            "memory_limit_text = (Path('/sys/fs/cgroup') / cgroup.lstrip('/') / "
            "'memory.max').read_text().strip()",
            "assert memory_limit_text != 'max'",
            "assert 0 < int(memory_limit_text) <= 4 * 1024 * 1024 * 1024",
            "assert Path('.git').is_file()",
            "assert Path('build').is_symlink()",
            "assert Path('pi-smoke-logs').is_dir()",
            f"assert Path('tracked.txt').read_text() == {expected_tracked!r}",
            f"assert subprocess.run(('git', 'rev-parse', 'HEAD'), capture_output=True, "
            f"text=True, check=True).stdout.strip() == {expected_head!r}",
            "scratch = Path(os.environ['HOME']).parent",
            "assert Path('build').resolve().is_relative_to(scratch)",
            "for writable in (scratch, Path('pi-smoke-logs')):",
            "    filesystem = os.statvfs(writable)",
            "    capacity = filesystem.f_frsize * filesystem.f_blocks",
            f"    assert 0 < capacity <= {PYXIS_WRITABLE_FILESYSTEM_MAX_BYTES}",
            "try:",
            "    Path('tracked.txt').write_text('changed')",
            "except OSError:",
            "    pass",
            "else:",
            "    raise SystemExit('source was writable')",
            "assert subprocess.run("
            "('git', 'config', '--local', 'pyxis.probe', '1')).returncode != 0",
            "Path('build/probe.txt').write_text('scratch')",
            "Path('coverage.xml').write_text('coverage')",
            "Path('pi-smoke-logs/probe.txt').write_text('logs')",
        )
    )


def test_pyxis_boundary_probe_is_valid_python() -> None:
    """The live boundary probe must compile before it uses a Slurm allocation."""
    program = _boundary_probe_program(
        expected_head="a" * 40,
        expected_job_id="1234",
        expected_tracked="immutable\n",
        timeout_s=300,
    )

    compile(program, "<pyxis-boundary-probe>", "exec")


def _active_slurm_steps(job_id: str) -> set[str]:
    """Return the active step IDs for one allocation."""
    squeue = shutil.which("squeue", path=os.defpath)
    if squeue is None:
        pytest.fail("live Pyxis cancellation evidence requires squeue")
    result = subprocess.run(
        (squeue, "--steps", "--noheader", "--jobs=" + job_id, "--format=%i"),
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": os.defpath, "SLURM_JOB_ID": job_id},
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _without_namespace_isolation(command: tuple[str, ...]) -> tuple[str, ...]:
    """Remove only the fixed nested namespace command for a negative control."""
    start = command.index("/usr/bin/unshare")
    expected = (
        "/usr/bin/unshare",
        "--user",
        "--map-current-user",
        "--net",
        "--ipc",
        "--uts",
        "--",
    )
    assert command[start : start + len(expected)] == expected
    return command[:start] + command[start + len(expected) :]


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

    srun_executable = resolve_trusted_srun_executable()
    missing = [name for name in ("enroot",) if shutil.which(name, path=os.defpath) is None]
    if srun_executable is None:
        missing.append("trusted /usr/bin/srun")
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
    authority = json.loads(Path(authority_text).read_text(encoding="utf-8"))
    if not isinstance(authority, dict):
        pytest.fail("live Pyxis authority must contain one object")
    quota_root = Path(quota_root_text)
    job_id = os.environ.get("SLURM_JOB_ID", "")
    if not job_id.isdecimal():
        pytest.fail("live Pyxis evidence requires an active Slurm allocation")

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _git(checkout, "init", "--initial-branch", "main")
    _git(checkout, "config", "user.name", "Pyxis integration")
    _git(checkout, "config", "user.email", "pyxis@example.invalid")
    (checkout / "tracked.txt").write_text("immutable\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-m", "fixture")
    head = _git(checkout, "rev-parse", "HEAD")

    timeout_s = 300
    program = _boundary_probe_program(
        expected_head=head,
        expected_job_id=job_id,
        expected_tracked="immutable\n",
        timeout_s=timeout_s,
    )
    compile(program, "<pyxis-boundary-probe>", "exec")

    job = BuildTestJob(
        repo="fixture/repo",
        cwd=checkout,
        argv=("uv", "run", "python", "-c", program),
        timeout_s=timeout_s,
        expected_head_sha=head,
        immutable_source=True,
    )
    run_pattern = "hephaestus-host-verification-run-*"
    runs_before = {path.name for path in quota_root.glob(run_pattern)}
    steps_before = _active_slurm_steps(job_id)
    cancelled = threading.Event()
    cancelled.set()
    cancelled_pool = WorkerPool(
        size=1,
        shutdown=cancelled,
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "cancelled-locks",
        host_verification_pyxis_image=image,
        host_verification_pyxis_sha256=expected_sha256,
        host_verification_pyxis_authority=Path(authority_text),
        host_verification_pyxis_quota_root=quota_root,
    )
    try:
        cancelled_result = cancelled_pool._run_build_test(job)
    finally:
        cancelled_pool.shutdown(mark_interrupted=False)
    assert cancelled_result.interrupted is True
    assert _active_slurm_steps(job_id) == steps_before
    assert {path.name for path in quota_root.glob(run_pattern)} == runs_before

    metadata = validate_pyxis_image(
        image,
        expected_sha256=expected_sha256,
        provenance=Path(authority_text),
    )
    assert srun_executable is not None
    with (
        tempfile.TemporaryDirectory(
            prefix=".hephaestus-pyxis-negative-", dir=image.parent
        ) as shared_text,
        tempfile.TemporaryDirectory(
            prefix="hephaestus-pyxis-negative-", dir=quota_root
        ) as quota_text,
    ):
        shared = Path(shared_text)
        negative_source = shared / "source"
        negative_metadata = shared / "metadata.git"
        negative_source.mkdir()
        negative_metadata.mkdir()
        (negative_source / "pi-smoke-logs").mkdir()
        quota_run = Path(quota_text)
        negative_scratch = quota_run / "scratch"
        negative_logs = quota_run / "pi-smoke-logs"
        negative_scratch.mkdir()
        negative_logs.mkdir()
        negative_command = build_pyxis_srun_command(
            srun_executable=Path(srun_executable),
            image=metadata,
            source=negative_source,
            git_metadata=negative_metadata,
            scratch=negative_scratch,
            pi_smoke_logs=negative_logs,
            argv=("/usr/local/bin/python", "-c", program),
            environment=build_pyxis_environment(
                source=negative_source,
                scratch=negative_scratch,
            ),
            timeout_s=timeout_s,
        )
        negative = subprocess.run(
            _linux_resource_limited_command(
                _without_namespace_isolation(negative_command), timeout_s=timeout_s
            ),
            cwd=negative_source,
            env={"PATH": os.defpath, "SLURM_JOB_ID": job_id},
            capture_output=True,
            text=True,
            timeout=timeout_s + 30,
            check=False,
        )
    assert negative.returncode != 0
    assert "nested namespaces were not created" in f"{negative.stdout}\n{negative.stderr}"

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        host_verification_pyxis_image=image,
        host_verification_pyxis_sha256=expected_sha256,
        host_verification_pyxis_authority=Path(authority_text),
        host_verification_pyxis_quota_root=quota_root,
    )
    try:
        result = pool._run_build_test(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is True, (
        f"Pyxis host verification failed: {result.error}\n{result.stderr_tail}"
    )
    assert result.value == {
        "container_image": result.value["container_image"],
        "container_image_sha256": expected_sha256,
        "container_image_id": authority["container_image_id"],
        "container_image_reference": authority["container_image_reference"],
        "containerfile_sha256": authority["containerfile_sha256"],
        "container_source_revision": authority["source_revision"],
        "container_runtime": "pyxis",
        "failure_kind": "none",
        "head_sha": head,
        "immutable_source": True,
        "platform": "linux",
        "status": "passed",
    }
    assert authority["squashfs_sha256"] == expected_sha256
    assert Path(result.value["container_image"]).name == f"sha256-{expected_sha256}.sqsh"
