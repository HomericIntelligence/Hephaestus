"""Check that live Pyxis evidence requires a reachable same-node control."""

import ast
import json
import subprocess
from unittest.mock import Mock

import pytest

from hephaestus.automation.pipeline.host_verification_pyxis import PyxisExecutionPlacement
from hephaestus.automation.pipeline.job_results import JobResult
from tests.integration import test_host_verification_pyxis_e2e as live


@pytest.mark.parametrize(
    "updates",
    [{"boot_id": "other-boot"}, {"hostname": "other-node"}, {"token": "other-listener"}],
)
def test_network_control_rejects_a_different_execution_context(updates: dict[str, str]) -> None:
    """A successful command on the wrong node or listener is not evidence."""
    value = {"boot_id": "this-boot", "hostname": "this-node", "token": "this-listener"}
    value.update(updates)
    result = subprocess.CompletedProcess(["srun"], 0, json.dumps(value), "")
    with pytest.raises(AssertionError, match="control"):
        live._validate_network_control(result, "this-boot", "this-node", "this-listener")


def test_network_control_accepts_matching_positive_evidence() -> None:
    """The command must prove the exact host boot and listener challenge."""
    value = {"boot_id": "this-boot", "hostname": "this-node", "token": "this-listener"}
    result = subprocess.CompletedProcess(["srun"], 0, json.dumps(value), "")
    live._validate_network_control(result, "this-boot", "this-node", "this-listener")


@pytest.mark.parametrize("code, output", [(1, "{}"), (0, "invalid"), (0, "{}"), (0, "x" * 4097)])
def test_network_control_rejects_failed_or_incomplete_evidence(code: int, output: str) -> None:
    """Failure and malformed output cannot establish control reachability."""
    result = subprocess.CompletedProcess(["srun"], code, output, "")
    with pytest.raises(AssertionError, match="control"):
        live._validate_network_control(result, "this-boot", "this-node", "this-listener")


def test_failed_control_prevents_candidate_execution() -> None:
    """Do not run the candidate after a control failure."""
    pool = Mock()
    control = Mock(side_effect=AssertionError("control failed"))
    with pytest.raises(AssertionError, match="control failed"):
        live._run_controlled_job(pool, Mock(), control)
    pool._run_build_test.assert_not_called()


def test_control_failure_after_candidate_invalidates_result() -> None:
    """A listener that disappears during the check invalidates the result."""
    pool = Mock()
    control = Mock(side_effect=[None, AssertionError("control failed")])
    with pytest.raises(AssertionError, match="control failed"):
        live._run_controlled_job(pool, Mock(), control)
    pool._run_build_test.assert_called_once()


def test_candidate_result_requires_controls_before_and_after() -> None:
    """Both positive controls surround the actual worker call."""
    calls: list[str] = []
    pool = Mock()
    expected = JobResult(ok=True)

    def run(job: object) -> JobResult:
        calls.append("worker")
        return expected

    pool._run_build_test.side_effect = run
    result = live._run_controlled_job(pool, Mock(), lambda: calls.append("control"))
    assert result is expected
    assert calls == ["control", "worker", "control"]


def test_positive_control_uses_explicit_placement_and_scrubbed_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control joins the same allocation and node without ambient options."""
    value = {"boot_id": "this-boot", "hostname": "this-node", "token": "this-listener"}
    run = Mock(return_value=subprocess.CompletedProcess(["srun"], 0, json.dumps(value), ""))
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("SLURM_JOB_ID", "999")
    monkeypatch.setenv("SRUN_CONTAINER_IMAGE", "untrusted")
    placement = PyxisExecutionPlacement("123", "node-1")
    live._same_node_network_control(
        "/usr/bin/srun", placement, 12345, "this-listener", "this-boot", "this-node"
    )
    argv = run.call_args.args[0]
    assert argv[0] == "/usr/bin/srun"
    ast.parse(argv[argv.index("-c") + 1])
    assert "--jobid=123" in argv and "--nodelist=node-1" in argv
    assert "--exclusive" in argv and "--export=NONE" in argv
    assert not any(arg.startswith("--container-") for arg in argv)
    assert "SLURM_JOB_ID" not in run.call_args.kwargs["env"]
    assert "SRUN_CONTAINER_IMAGE" not in run.call_args.kwargs["env"]
    assert run.call_args.kwargs["timeout"] == 30
