"""Type-check tests for the static coordinator host contract."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hephaestus.utils.helpers import NETWORK_TIMEOUT


def _run_mypy(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    """Run mypy for one isolated coordinator contract probe."""
    probe = tmp_path / "coordinator_contract_probe.py"
    probe.write_text(source, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "mypy", "--cache-dir", str(tmp_path / "mypy-cache"), str(probe)],
        check=False,
        capture_output=True,
        text=True,
        timeout=NETWORK_TIMEOUT,
    )


def test_coordinator_satisfies_host_protocol(tmp_path: Path) -> None:
    """Accept the concrete coordinator where the host protocol is required."""
    result = _run_mypy(
        tmp_path,
        """
from hephaestus.automation.pipeline.coordinator import Coordinator
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost

def require_host(host: _CoordinatorHost) -> None:
    pass

def check(coordinator: Coordinator) -> None:
    require_host(coordinator)
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_host_protocol_declares_shared_members(tmp_path: Path) -> None:
    """Type-check all fields and methods added for fixed collaborators."""
    result = _run_mypy(
        tmp_path,
        """
from typing import Any, assert_type
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost
from hephaestus.automation.pipeline.coordinator_types import RepoIssueSource, WorkItem
from hephaestus.automation.pipeline.jobs import JobHandle, JobResult
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.seeding import SeedEntry
from hephaestus.automation.pipeline.stages import JobRequest, StageContext, StageGitHub

def check(host: _CoordinatorHost, item: WorkItem, request: JobRequest, result: JobResult,
          source: RepoIssueSource, github: StageGitHub, job: object) -> None:
    assert_type(host.auxiliary_pool, Any)
    assert_type(host.auxiliary_completion_q.empty(), bool)
    assert_type(host.auxiliary_in_flight, dict[JobHandle, WorkItem])
    assert_type(host._learning_work_permit_ids, set[int])
    assert_type(host._auxiliary_pool_separate, bool)
    assert_type(host._direct_scope_bootstrap_pending, bool)
    assert_type(host._immediate, bool)
    assert_type(host._progress, bool)
    assert_type(host._fatal, bool)
    assert_type(host._auxiliary_job_failure_count, int)
    assert_type(host._pass_work_count, int)
    assert_type(host._stalled_ticks, int)
    assert_type(host._grace_deadline, float | None)
    assert_type(host._is_auxiliary_stage(StageName.LEARNING), bool)
    assert_type(host._lane_handoff_capacity(item, StageName.FINISHED), bool)
    host._persist_learning_intents(item)
    host._submit(item, request)
    host._drain_completions()
    host._wait_for_completion(0.1)
    assert_type(host._ctx_for(item), StageContext)
    host._park_resumable(item)
    host._timer_park(item, 0.1)
    assert_type(host._job_result_event_fields(result), dict[str, Any])
    host._register_pipeline_writer_worktree(item, job, result)
    assert_type(
        host._scope_seed_decision(1, None, "reason", None),
        tuple[StageName | None, str, bool],
    )
    assert_type(host._classify_repo_issue_entry("repo", source, 1, github), SeedEntry | None)
    host._restore_learning_intents(item, StageName.PLANNING, "reason")
    assert_type(host._direct_issue_identity("repo", 1, "nonce"), tuple[int | None, str])
""",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_host_protocol_rejects_unknown_member(tmp_path: Path) -> None:
    """Reject a member that is not part of the static host contract."""
    result = _run_mypy(
        tmp_path,
        """
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost

def check(host: _CoordinatorHost) -> None:
    host.issue_3052_unknown_member
""",
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert '"_CoordinatorHost" has no attribute "issue_3052_unknown_member"' in output
    assert "[attr-defined]" in output


def test_host_protocol_rejects_incorrect_stage_argument(tmp_path: Path) -> None:
    """Reject a string where an auxiliary stage name is required."""
    result = _run_mypy(
        tmp_path,
        """
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost

def check(host: _CoordinatorHost) -> None:
    host._is_auxiliary_stage("learning")
""",
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert 'incompatible type "str"' in output
    assert "[arg-type]" in output


def test_host_protocol_rejects_incorrect_permit_element(tmp_path: Path) -> None:
    """Reject a string in the integer learning-permit set."""
    result = _run_mypy(
        tmp_path,
        """
from hephaestus.automation.pipeline.coordinator_contract import _CoordinatorHost

def check(host: _CoordinatorHost) -> None:
    host._learning_work_permit_ids.add("bad")
""",
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert 'incompatible type "str"' in output
    assert "[arg-type]" in output
