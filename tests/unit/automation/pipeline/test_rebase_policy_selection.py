"""Tests for repository-specific rebase validation policy selection."""

from __future__ import annotations

import importlib
import queue
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
from hephaestus.automation.pipeline.worker_pool import WorkerPool


def _selector() -> object:
    module = importlib.import_module("hephaestus.automation.pipeline.rebase_adr_policy")
    return getattr(module, "select_rebase_policy", None)


def _continue_job(tmp_path: Path, *, repo: str) -> GitJob:
    return GitJob(
        repo=repo,
        op="continue_rebase",
        timeout_s=60,
        kwargs={
            "cwd": tmp_path,
            "remote": "origin",
            "branch": "7-auto-impl",
            "base_sha": "b" * 40,
            "expected_remote_sha": "a" * 40,
            "conflict_paths": ("x.py",),
            "conflict_snapshot": {"x.py": "before"},
            "conflict_index_snapshot": "1" * 64,
            "paused_head_sha": "c" * 40,
        },
    )


def _fake_git_run(argv: list[str], **_kwargs: object) -> MagicMock:
    if argv == ["git", "diff", "--name-only", "-z"]:
        return MagicMock(returncode=0, stdout="x.py\0")
    if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
        return MagicMock(returncode=0, stdout="")
    if argv[:3] == ["git", "rev-list", "--reverse"]:
        return MagicMock(returncode=0, stdout="c" * 40)
    if argv[:3] == ["git", "cat-file", "-p"]:
        return MagicMock(
            returncode=0,
            stdout=(
                "tree deadbeef\ngpgsig signature\n\nfix\n\n"
                "Signed-off-by: Test User <test@example.com>\n"
            ),
        )
    return MagicMock(returncode=0, stdout="")


def _run_continuation(
    pool: WorkerPool,
    job: GitJob,
    *,
    structural: JobResult | None,
    semantic: JobResult | None = None,
    selected: bool = True,
    structural_test_argv: tuple[str, ...] | None = ("tests/unit/docs/test_adr_records.py",),
    policy_selector: Callable[[str], RebaseValidationPolicy | None] | None = None,
) -> JobResult:
    policy = (
        RebaseValidationPolicy(
            name="hephaestus-adr-v1",
            semantic_validator=lambda _cwd: semantic,
            structural_test_argv=structural_test_argv,
        )
        if selected
        else None
    )
    with (
        patch.object(pool, "_read_remote_branch_head", return_value="a" * 40),
        patch.object(
            pool,
            "_conflict_receipt",
            return_value={
                "conflict_paths": ("x.py",),
                "conflict_snapshot": {"x.py": "after"},
                "conflict_index_snapshot": "1" * 64,
                "paused_head_sha": "c" * 40,
            },
        ),
        patch(
            "hephaestus.automation.pipeline.worker_pool._controlled_git_signing_env",
            return_value={},
        ),
        patch.object(pool, "_run_immutable_build_test", return_value=structural),
        patch.object(
            pool,
            "_select_rebase_policy",
            side_effect=policy_selector or (lambda _repo: policy),
        ),
        patch.object(
            pool,
            "_authenticated_remote_revalidator",
            return_value=lambda: ({}, ()),
        ),
        patch.object(pool, "_read_publish_head", return_value="d" * 40),
        patch("hephaestus.automation.pipeline.worker_pool.git_utils.push_head_to_branch") as push,
        patch(
            "hephaestus.automation.pipeline.worker_pool.git_utils.run",
            side_effect=_fake_git_run,
        ),
    ):
        result = pool._git_continue_rebase(job)
    if result.ok:
        push.assert_called_once()
    else:
        push.assert_not_called()
    return result


def test_selector_is_case_insensitive_and_exact() -> None:
    """The host selector matches only the Hephaestus repository identity."""
    selector = _selector()
    assert callable(selector)
    bound = partial(selector, "HomericIntelligence")

    selected = bound("hEpHaEsTuS")
    assert selected is not None
    assert selected.name == "hephaestus-adr-v1"
    with pytest.raises(AttributeError):
        selected.name = "other"
    assert bound("Comet") is None
    assert bound("Hephaestus-extra") is None
    assert partial(selector, "OtherOrg")("Hephaestus") is None


def test_selected_policy_rejects_readme_index_drift(tmp_path: Path) -> None:
    """A selected policy rejects an ADR README index that is out of sync."""
    selector = _selector()
    assert callable(selector)
    policy = partial(selector, "HomericIntelligence")("Hephaestus")
    assert policy is not None
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-first-decision.md").write_text(
        "# ADR-0001: First decision\n"
        "- Status: Accepted\n"
        "- Date: 2026-01-01\n\n"
        "## Context\nA context.\n\n"
        "## Decision\nA decision.\n\n"
        "## Alternatives considered\nAn alternative.\n\n"
        "## Consequences\nA consequence.\n",
        encoding="utf-8",
    )
    (adr_dir / "README.md").write_text("- [Old decision](0002-old-decision.md)\n", encoding="utf-8")

    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        result = pool._validate_rebased_tree(tmp_path, policy=policy)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result is not None
    assert result.ok is False
    assert result.value == {
        "failure_kind": "semantic_validation",
        "rebase_policy": "hephaestus-adr-v1",
    }
    assert "README index out of sync" in (result.error or "")


def test_ordinary_rebase_does_not_select_a_conflict_policy(tmp_path: Path) -> None:
    """An ordinary rebase keeps its mechanical result without policy selection."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    job = GitJob(
        repo="Hephaestus",
        op="rebase",
        timeout_s=60,
        kwargs={"cwd": tmp_path, "base_branch": "main"},
    )
    try:
        with (
            patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
            patch(
                "hephaestus.automation.pipeline.worker_pool._required_git_signing_env",
                return_value={},
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.rebase_worktree_onto",
                return_value=True,
            ),
            patch.object(pool, "_select_rebase_policy", return_value=None) as select,
        ):
            result = pool._git_rebase(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result == JobResult(ok=True, value=True)
    select.assert_not_called()


def test_unconfigured_target_continuation_skips_policy_gates(tmp_path: Path) -> None:
    """A target without a policy publishes a valid nonstandard ADR layout."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        (adr_dir / "index.md").write_text("# Index\n", encoding="utf-8")
        (adr_dir / "0000-template.md").write_text("# Template\n", encoding="utf-8")
        (adr_dir / "0001-fleet-routing.md").write_text("# Fleet routing\n", encoding="utf-8")
        selector = _selector()
        assert callable(selector)
        production_selector = partial(selector, "HomericIntelligence")
        assert production_selector("Comet") is None
        result = _run_continuation(
            pool,
            _continue_job(tmp_path, repo="Comet"),
            structural=None,
            selected=False,
            policy_selector=production_selector,
        )
        assert result.ok is True
        assert result.value.get("rebase_policy") is None
    finally:
        pool.shutdown(mark_interrupted=False)


def test_selected_structural_failure_keeps_diagnostics_and_policy(tmp_path: Path) -> None:
    """A selected structural failure blocks publication and keeps diagnostics."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        selector = _selector()
        assert callable(selector)
        pool._rebase_policy_selector = partial(selector, "HomericIntelligence")
        test_path = tmp_path / "tests" / "unit" / "docs" / "test_adr_records.py"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("# structural test\n", encoding="utf-8")
        failed = JobResult(
            ok=False,
            value={"failure_kind": "validation", "cause": "duplicate ADR number"},
            stdout_tail="stdout tail",
            stderr_tail="stderr tail",
            error="pytest failed",
        )
        result = _run_continuation(
            pool,
            _continue_job(tmp_path, repo="Hephaestus"),
            structural=failed,
        )
        assert result.ok is False
        assert result.value["failure_kind"] == "validation"
        assert result.value["cause"] == "duplicate ADR number"
        assert result.value["rebase_policy"] == "hephaestus-adr-v1"
        assert result.stdout_tail == "stdout tail"
        assert result.stderr_tail == "stderr tail"
        assert "hephaestus-adr-v1" in (result.error or "")
        assert "pytest failed" in (result.error or "")
    finally:
        pool.shutdown(mark_interrupted=False)


def test_selected_semantic_failure_keeps_cause_and_policy(tmp_path: Path) -> None:
    """A selected semantic failure blocks publication and keeps its cause."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        selector = _selector()
        assert callable(selector)
        pool._rebase_policy_selector = partial(selector, "HomericIntelligence")
        test_path = tmp_path / "tests" / "unit" / "docs" / "test_adr_records.py"
        test_path.parent.mkdir(parents=True)
        test_path.write_text("# structural test\n", encoding="utf-8")
        failed = JobResult(
            ok=False,
            value={"failure_kind": "semantic_validation", "cause": "duplicate ADR number"},
            stdout_tail="semantic stdout",
            stderr_tail="semantic stderr",
            error="semantic validator failed",
        )
        result = _run_continuation(
            pool,
            _continue_job(tmp_path, repo="Hephaestus"),
            structural=JobResult(ok=True),
            semantic=failed,
        )
        assert result.ok is False
        assert result.value["failure_kind"] == "semantic_validation"
        assert result.value["cause"] == "duplicate ADR number"
        assert result.value["rebase_policy"] == "hephaestus-adr-v1"
        assert result.stdout_tail == "semantic stdout"
        assert result.stderr_tail == "semantic stderr"
        assert "hephaestus-adr-v1" in (result.error or "")
        assert "semantic validator failed" in (result.error or "")
    finally:
        pool.shutdown(mark_interrupted=False)


@pytest.mark.parametrize(
    ("structural_test_argv", "expected_detail"),
    [
        (None, "command is missing"),
        ((), "command is missing"),
        (("uv", "run", "pytest"), "test is missing"),
        (
            ("uv", "run", "pytest", "tests/unit/docs/test_adr_records.py"),
            "not in the tree",
        ),
    ],
)
def test_selected_policy_missing_structural_test_blocks_publication(
    tmp_path: Path,
    structural_test_argv: tuple[str, ...] | None,
    expected_detail: str,
) -> None:
    """A selected policy fails when its structural command is incomplete."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
    )
    try:
        result = _run_continuation(
            pool,
            _continue_job(tmp_path, repo="Hephaestus"),
            structural=None,
            structural_test_argv=structural_test_argv,
        )
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result.ok is False
    assert result.value == {
        "failure_kind": "validation_runner",
        "rebase_policy": "hephaestus-adr-v1",
    }
    assert expected_detail in (result.error or "")
