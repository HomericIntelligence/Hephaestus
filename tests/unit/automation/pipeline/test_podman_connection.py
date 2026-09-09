"""The selected Podman connection reaches only the verified local CI runner."""

import queue
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.automation.pipeline.jobs import BuildTestJob, JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool


@pytest.mark.parametrize("selected", [None, "hephaestus-ci"])
@pytest.mark.parametrize("verified", [False, True])
def test_explicit_connection_reaches_only_verified_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selected: str | None, verified: bool
) -> None:
    """Ambient engine and connection values cannot select the runner target."""
    monkeypatch.setenv("CONTAINER_CONNECTION", "stopped-default")
    monkeypatch.setenv("CONTAINER_ENGINE", "untrusted-engine")
    pool = WorkerPool(
        size=1, shutdown=threading.Event(), completion_q=queue.Queue(), podman_machine=selected
    )
    job = BuildTestJob(
        repo="repo",
        cwd=tmp_path,
        argv=("bash", "scripts/run_ci_local.sh", "all", "--rebuild"),
        timeout_s=60,
        verified_runner_source_revision="a" * 40 if verified else None,
    )
    try:
        with patch(
            "hephaestus.automation.pipeline.worker_pool.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "ok", ""),
        ) as run:
            result = pool._run_build_test(job)
        assert result.ok
        env = run.call_args.kwargs["env"]
        if selected and verified:
            assert env["CONTAINER_CONNECTION"] == selected
            assert env["CONTAINER_ENGINE"] == "podman"
        else:
            assert "CONTAINER_CONNECTION" not in env
            assert "CONTAINER_ENGINE" not in env
    finally:
        pool.shutdown(mark_interrupted=False)


def test_selected_connection_does_not_change_immutable_verification(tmp_path: Path) -> None:
    """Immutable host verification retains its separate execution boundary."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        podman_machine="hephaestus-ci",
    )
    job = BuildTestJob(
        repo="repo",
        cwd=tmp_path,
        argv=("uv", "run", "pytest"),
        timeout_s=60,
        expected_head_sha="a" * 40,
        immutable_source=True,
    )
    try:
        with patch.object(
            pool, "_run_immutable_build_test", return_value=JobResult(ok=True)
        ) as immutable:
            assert pool._run_build_test(job).ok
        immutable.assert_called_once_with(job)
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize("name", ["", "--remote", "bad/name", "a\nname", "a" * 129])
def test_worker_rejects_invalid_explicit_machine_name(name: str) -> None:
    """Invalid configuration fails before the worker can execute a job."""
    from hephaestus.automation.podman_machine_supervisor import PodmanMachineError

    with pytest.raises(PodmanMachineError, match="Invalid Podman machine name"):
        WorkerPool(
            size=1, shutdown=threading.Event(), completion_q=queue.Queue(), podman_machine=name
        )
