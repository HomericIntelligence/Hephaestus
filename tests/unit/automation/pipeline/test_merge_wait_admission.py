"""Tests for repository and branch identity in merge-wait admission."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.merge_wait_admission import (
    MergeWaitAdmissionSnapshot,
    VerifiedRepositoryDefaultBranch,
    validate_merge_wait_admission,
)


def _repository(default_branch: str = "main") -> VerifiedRepositoryDefaultBranch:
    """Return one complete verified repository record."""
    return VerifiedRepositoryDefaultBranch(
        owner="HomericIntelligence",
        name="Hephaestus",
        name_with_owner="HomericIntelligence/Hephaestus",
        default_branch=default_branch,
    )


def _state(*, base: str = "main", head: str = "a" * 40) -> dict[str, object]:
    """Return the branch and head fields used by the pure admission check."""
    return {"baseRefName": base, "headRefOid": head}


@pytest.mark.parametrize("branch", ["main", "master", "trunk"])
def test_initial_admission_accepts_the_exact_verified_default_branch(branch: str) -> None:
    """Default-branch spelling does not change merge eligibility."""
    result = validate_merge_wait_admission(
        _state(base=branch),
        _repository(branch),
        "a" * 40,
    )

    assert result == MergeWaitAdmissionSnapshot(_repository(branch), branch, "a" * 40)


@pytest.mark.parametrize("base", ["main", "release"])
def test_initial_admission_rejects_a_non_default_base(base: str) -> None:
    """Release and stacked bases do not inherit default-branch authority."""
    assert (
        validate_merge_wait_admission(_state(base=base), _repository("master"), "a" * 40)
        == "non_default_base"
    )


@pytest.mark.parametrize(
    ("state", "repository", "outcome"),
    [
        (None, _repository(), "pr_state_unverified"),
        ({"baseRefName": "", "headRefOid": "a" * 40}, _repository(), "pr_state_unverified"),
        ({"baseRefName": "main", "headRefOid": ""}, _repository(), "missing_pr_head"),
        (_state(), None, "default_branch_unavailable"),
    ],
)
def test_initial_admission_fails_closed_on_incomplete_facts(
    state: object, repository: object, outcome: str
) -> None:
    """Incomplete branch facts cannot create a merge snapshot."""
    assert validate_merge_wait_admission(state, repository, "a" * 40) == outcome


@pytest.mark.parametrize(
    ("current_repository", "current_state", "outcome"),
    [
        (
            VerifiedRepositoryDefaultBranch("Other", "Hephaestus", "Other/Hephaestus", "main"),
            _state(),
            "repository_identity_drift",
        ),
        (_repository("trunk"), _state(), "default_branch_drift"),
        (_repository(), _state(base="release"), "pr_base_drift"),
        (_repository(), _state(head="b" * 40), "reviewed_head_drift"),
    ],
)
def test_final_admission_rejects_each_snapshot_drift(
    current_repository: VerifiedRepositoryDefaultBranch,
    current_state: dict[str, object],
    outcome: str,
) -> None:
    """Every identity change invalidates the initial admission snapshot."""
    initial = MergeWaitAdmissionSnapshot(_repository(), "main", "a" * 40)

    assert (
        validate_merge_wait_admission(
            current_state,
            current_repository,
            "a" * 40,
            initial=initial,
        )
        == outcome
    )


@pytest.mark.parametrize(
    "values",
    [
        ("", "Hephaestus", "/Hephaestus", "main"),
        ("HomericIntelligence", "", "HomericIntelligence/", "main"),
        ("HomericIntelligence", "Hephaestus", "Other/Hephaestus", "main"),
        ("HomericIntelligence", "Hephaestus", "HomericIntelligence/Hephaestus", " main"),
    ],
)
def test_verified_repository_rejects_malformed_identity(values: tuple[str, str, str, str]) -> None:
    """The typed boundary rejects incomplete or inconsistent metadata."""
    with pytest.raises(ValueError, match="metadata was malformed"):
        VerifiedRepositoryDefaultBranch(*values)
