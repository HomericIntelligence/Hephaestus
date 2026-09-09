"""Tests for the Hephaestus rebase validation policy."""

from __future__ import annotations

import importlib
import threading
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path
from time import monotonic
from typing import cast
from unittest.mock import ANY, MagicMock, patch

import pytest

from hephaestus.automation.pipeline.job_results import JobResult
from hephaestus.automation.pipeline.jobs import GitJob
from hephaestus.automation.pipeline.queues import CompletionQueue
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


def _mutate_policy_name(policy: RebaseValidationPolicy) -> None:
    """Attempt the mutation that the frozen policy must reject."""
    policy.name = "other"  # type: ignore[misc]


def test_policy_selector_matches_the_exact_hephaestus_repository() -> None:
    """The selector applies the ADR policy to the Hephaestus identity."""
    selector = _selector()
    bound = partial(selector, "HomericIntelligence")

    selected = bound("hEpHaEsTuS")
    assert selected is not None
    assert selected.name == "hephaestus-adr-v1"
    with pytest.raises(FrozenInstanceError):
        _mutate_policy_name(selected)
    assert bound("Hephaestus-extra") is None
    assert partial(selector, "OtherOrg")("Hephaestus") is None


def test_policy_selector_allows_current_head_fallback_only_for_mnemosyne() -> None:
    """Only Mnemosyne can continue after a writer rebase conflict."""
    selector = _selector()

    mnemosyne = selector("HomericIntelligence", "Mnemosyne")
    hephaestus = selector("HomericIntelligence", "Hephaestus")

    assert mnemosyne is not None
    assert mnemosyne.name == "mnemosyne-current-head-v1"
    assert mnemosyne.allow_unrebased_writer_fallback is True
    assert hephaestus is not None
    assert hephaestus.allow_unrebased_writer_fallback is False


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


@pytest.mark.parametrize("publish", [False, True])
def test_manual_rebase_without_conflicts_does_not_run_repository_policy(
    tmp_path: Path, publish: bool
) -> None:
    """A successful manual replay does not run conflict-only validation."""
    pool = WorkerPool(
        size=1,
        shutdown=threading.Event(),
        completion_q=CompletionQueue(),
        lock_dir=tmp_path / "locks",
    )
    head, base, rewritten = "a" * 40, "b" * 40, "c" * 40
    job = GitJob(
        repo="Hephaestus",
        expected_repository="HomericIntelligence/Hephaestus",
        op="rebase",
        timeout_s=60,
        deadline_s=monotonic() + 60,
        kwargs={
            "cwd": tmp_path,
            "rebase_reason": "manual",
            "publish_rebased_head": publish,
            "branch": "7-auto-impl",
            "expected_remote_sha": head,
            "expected_head_sha": head,
        },
    )
    try:
        with (
            patch.object(pool, "_sync_writer_to_expected_remote_head", return_value=None),
            patch.object(pool, "_authenticated_remote_revalidator", return_value=lambda: ({}, ())),
            patch.object(
                pool, "_git_fetch_main", return_value=JobResult(ok=True, value={"head_sha": base})
            ),
            patch.object(pool, "_read_publish_head", side_effect=[head, head, rewritten]),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.is_clean_working_tree",
                return_value=True,
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.run",
                return_value=MagicMock(returncode=1),
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool._required_git_signing_env",
                return_value={},
            ),
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.rebase_worktree_onto",
                return_value=True,
            ) as rebase,
            patch(
                "hephaestus.automation.pipeline.worker_pool.git_utils.push_head_to_branch"
            ) as push,
            patch.object(pool, "_select_rebase_policy") as select,
            patch.object(pool, "_run_rebase_structural_validation") as structural,
            patch.object(pool, "_validate_rebased_tree") as semantic,
        ):
            result = pool._git_rebase_once(job, record_source=MagicMock())
    finally:
        pool.shutdown(mark_interrupted=False)

    assert result == JobResult(
        ok=True, value={"rebased": True, "published": publish, "head_sha": rewritten}
    )
    select.assert_not_called()
    structural.assert_not_called()
    semantic.assert_not_called()
    assert rebase.call_args.kwargs["base_sha"] == base
    assert rebase.call_args.kwargs["preserve_conflicts"] is False
    assert push.call_count == int(publish)
    if publish:
        push.assert_called_once_with(
            "7-auto-impl",
            head,
            tmp_path,
            source_sha=rewritten,
            timeout=ANY,
            env={},
            remote_config=(),
            revalidate_remote=ANY,
        )
        assert 0 < push.call_args.kwargs["timeout"] <= 60
