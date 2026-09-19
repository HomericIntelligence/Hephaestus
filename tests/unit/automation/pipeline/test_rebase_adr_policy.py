"""Tests for the Hephaestus rebase validation policy."""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from hephaestus.automation.pipeline.rebase_policy import RebaseValidationPolicy
from tests.unit.automation.pipeline.test_rebase_policy import real_policy_case
from tests.unit.automation.test_rebase_recovery import _store
from tests.unit.automation.test_source_worktree import _git

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
def test_manual_rebase_without_selected_policy_needs_no_validation_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    publish: bool,
) -> None:
    """No selected policy means no validation execution, not absent source ownership."""
    with real_policy_case(tmp_path, monkeypatch, publish=publish) as case:
        selected: list[str] = []

        def no_policy(repository: str) -> None:
            selected.append(repository)
            return None

        monkeypatch.setattr(case.pool, "_rebase_policy_selector", no_policy)
        result = case.pool._run_git(case.job)
        assert result.ok, result
        assert selected and set(selected) == {"repository"}
        assert case.starts == [True]
        assert result.value["rebased"] is True
        assert result.value["published"] is publish
        assert result.value["head_sha"] == _git(case.binding.cwd, "rev-parse", "HEAD")
        assert result.value["head_sha"] != case.original
        assert _git(case.binding.cwd, "merge-base", "--is-ancestor", case.base, "HEAD") == ""
        assert "capability_receipt" not in result.value
        assert "structural_execution_receipt" not in result.value
        assert result.value["source_workspace"]["revision"] == result.value["head_sha"]
        case.backend.preflight.assert_not_called()
        assert case.checks == []
        assert case.events == (["push"] if publish else [])
        record = _store(case.manager.common_dir).read(1, case.job.capability_target.request_id)
        assert record.phase == "complete"
        assert record.policy_name is None
        assert record.request == case.job.capability_target
