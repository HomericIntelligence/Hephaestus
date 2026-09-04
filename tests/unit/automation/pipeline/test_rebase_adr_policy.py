"""Tests for the Hephaestus rebase validation policy."""

from __future__ import annotations

import importlib
import queue
import threading
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast
from unittest.mock import ANY, MagicMock, patch

import pytest

from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
from hephaestus.automation.pipeline.worker_pool import WorkerPool

RebasePolicyFactory = Callable[[str, str | None], RebaseValidationPolicy | None]


def _selector() -> RebasePolicyFactory:
    """Load the product-owned selector through its module boundary."""
    module = importlib.import_module("hephaestus.automation.pipeline.rebase_adr_policy")
    candidate = getattr(module, "select_rebase_policy", None)
    assert callable(candidate)
    return cast(RebasePolicyFactory, candidate)


def _hephaestus_policy() -> RebaseValidationPolicy:
    """Return the policy through the production repository selector."""
    policy = _selector()("HomericIntelligence", "Hephaestus")
    assert policy is not None
    return policy


def _write_valid_adr(path: Path) -> None:
    """Write one minimum valid Hephaestus ADR record."""
    path.write_text(
        "# ADR-0001: First decision\n"
        "- Status: Accepted\n"
        "- Date: 2026-01-01\n\n"
        "## Context\nA context.\n\n"
        "## Decision\nA decision.\n\n"
        "## Alternatives considered\nAn alternative.\n\n"
        "## Consequences\nA consequence.\n",
        encoding="utf-8",
    )


def test_policy_selector_matches_only_hephaestus_repository() -> None:
    """The selector accepts only the exact Hephaestus repository identity."""
    selector = _selector()
    bound = partial(selector, "HomericIntelligence")

    selected = bound("hEpHaEsTuS")
    assert selected is not None
    assert selected.name == "hephaestus-adr-v1"
    with pytest.raises(AttributeError):
        selected.name = "other"
    assert bound("Hephaestus-extra") is None
    assert partial(selector, "OtherOrg")("Hephaestus") is None


def test_policy_selector_returns_none_for_unconfigured_target() -> None:
    """The selector does not apply Hephaestus policy to another target."""
    selector = _selector()

    assert selector("HomericIntelligence", "Comet") is None
    assert selector("HomericIntelligence/Comet", None) is None


def test_hephaestus_policy_rejects_duplicate_numbers(tmp_path: Path) -> None:
    """The selected policy rejects duplicate ADR numbers."""
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0027-first-decision.md").write_text("# First\n", encoding="utf-8")
    (adr_dir / "0027-second-decision.md").write_text("# Second\n", encoding="utf-8")

    result = _hephaestus_policy().semantic_validator(tmp_path)

    assert result is not None
    assert result.ok is False
    assert result.value == {"failure_kind": "semantic_validation"}
    assert "duplicate ADR number 0027" in (result.error or "")


def test_hephaestus_policy_rejects_malformed_record(tmp_path: Path) -> None:
    """The selected policy rejects an incomplete ADR record."""
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-first-decision.md").write_text(
        "# ADR-0001: First decision\n- Status: Accepted\n",
        encoding="utf-8",
    )

    result = _hephaestus_policy().semantic_validator(tmp_path)

    assert result is not None
    assert result.ok is False
    assert result.value == {"failure_kind": "semantic_validation"}
    assert "malformed ADR record 0001-first-decision.md" in (result.error or "")


def test_hephaestus_policy_rejects_readme_index_drift(tmp_path: Path) -> None:
    """The selected policy rejects an ADR README index that is out of sync."""
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    _write_valid_adr(adr_dir / "0001-first-decision.md")
    (adr_dir / "README.md").write_text(
        "- [Old decision](0002-old-decision.md)\n",
        encoding="utf-8",
    )

    result = _hephaestus_policy().semantic_validator(tmp_path)

    assert result is not None
    assert result.ok is False
    assert result.value == {"failure_kind": "semantic_validation"}
    assert "README index out of sync" in (result.error or "")


def test_ordinary_rebase_does_not_select_a_conflict_policy(tmp_path: Path) -> None:
    """An ordinary rebase does not select a conflict-only policy."""
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


def test_published_ordinary_rebase_does_not_run_repository_policy(tmp_path: Path) -> None:
    """An ordinary published rebase does not run conflict-only policy."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=queue.Queue(),
        lock_dir=tmp_path / "locks",
        rebase_policy_selector=lambda _repo: RebaseValidationPolicy(
            name="hephaestus-adr-v1",
            semantic_validator=lambda _cwd: JobResult(
                ok=False,
                value={"failure_kind": "semantic_validation"},
                error="policy must not run",
            ),
            structural_test_argv=("tests/unit/docs/test_adr_records.py",),
        ),
    )
    job = GitJob(
        repo="Hephaestus",
        expected_repository="HomericIntelligence/Hephaestus",
        op="rebase",
        timeout_s=60,
        kwargs={
            "cwd": tmp_path,
            "base_branch": "main",
            "remote": "origin",
            "publish_rebased_head": True,
            "branch": "7-auto-impl",
            "expected_remote_sha": "a" * 40,
        },
    )
    try:
        with (
            patch.object(
                pool,
                "_authenticated_remote_git_configuration",
                return_value=({"AUTH": "fresh"}, ()),
            ),
            patch.object(
                pool,
                "_authenticated_remote_revalidator",
                return_value=lambda: ({"AUTH": "initial"}, ()),
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool._required_git_signing_env",
                return_value={"SIGNING": "required"},
            ) as signing,
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.run",
                side_effect=(MagicMock(returncode=0), MagicMock(returncode=1)),
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.rebase_worktree_onto",
                return_value=True,
            ) as rebase,
            patch.object(pool, "_select_rebase_policy", wraps=pool._select_rebase_policy) as select,
            patch.object(pool, "_run_rebase_structural_validation") as structural,
            patch.object(pool, "_validate_rebased_tree") as semantic,
            patch.object(pool, "_read_publish_head", return_value="b" * 40),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.push_head_to_branch"
            ) as push,
        ):
            result = pool._git_rebase(job)
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result == JobResult(
        ok=True,
        value={"rebased": True, "published": True, "head_sha": "b" * 40},
    )
    signing.assert_called_once_with(tmp_path, timeout=60)
    select.assert_not_called()
    structural.assert_not_called()
    semantic.assert_not_called()
    rebase.assert_called_once_with(
        cwd=tmp_path,
        base_branch="main",
        remote="origin",
        preserve_conflicts=True,
        timeout=60,
        env={"SIGNING": "required"},
        fetch_env={"AUTH": "initial"},
        fetch_config=(),
    )
    push.assert_called_once_with(
        "7-auto-impl",
        "a" * 40,
        tmp_path,
        source_sha="b" * 40,
        timeout=60,
        env={"AUTH": "fresh"},
        remote_config=(),
        revalidate_remote=ANY,
    )
