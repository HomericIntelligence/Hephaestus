"""Tests for repository and branch identity in merge-wait admission."""

from __future__ import annotations

import pytest

from hephaestus.automation.pipeline.merge_wait_admission import (
    MergeWaitAdmissionSnapshot,
    RequiredChecksDeferred,
    RequiredChecksEvidence,
    RequiredChecksGate,
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


class TestRequiredChecksEvidence:
    """Validation for the bounded evidence dataclass."""

    def test_valid_evidence_constructs(self) -> None:
        """A complete valid record builds without error."""
        ev = RequiredChecksEvidence(
            boundary="nonpassing_check_run",
            contexts=("required-ci",),
            detail="conclusion=failure",
        )
        assert ev.boundary == "nonpassing_check_run"
        assert ev.contexts == ("required-ci",)
        assert ev.detail == "conclusion=failure"

    def test_frozen_mutation_rejected(self) -> None:
        """The dataclass is immutable."""
        ev = RequiredChecksEvidence(
            boundary="nonpassing_check_run",
            contexts=("required-ci",),
            detail="conclusion=failure",
        )
        with pytest.raises(AttributeError):
            ev.boundary = "other"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "boundary",
        [
            "invalid_request",
            "read_unavailable",
            "snapshot_unverifiable",
            "unstable_snapshot",
            "pending_check_run",
            "nonpassing_check_run",
            "missing_requirement",
            "no_required_checks",
        ],
    )
    def test_boundary_accepts_all_closed_vocabulary(self, boundary: str) -> None:
        """Every enumerated boundary value is accepted."""
        RequiredChecksEvidence(boundary=boundary, contexts=(), detail="ok")

    def test_boundary_rejects_unknown_value(self) -> None:
        """A value outside the frozenset is rejected."""
        with pytest.raises(ValueError, match="boundary must be one of"):
            RequiredChecksEvidence(boundary="unknown_boundary", contexts=(), detail="ok")

    def test_contexts_rejects_more_than_eight(self) -> None:
        """Nine contexts exceed the bound."""
        with pytest.raises(ValueError, match="at most eight"):
            RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=tuple(f"ctx{i}" for i in range(9)),
                detail="ok",
            )

    def test_contexts_rejects_overlong_string(self) -> None:
        """An 81-character context string is rejected."""
        with pytest.raises(ValueError, match="at most 80 characters"):
            RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=("x" * 81,),
                detail="ok",
            )

    def test_contexts_rejects_non_string(self) -> None:
        """A non-string context entry is rejected."""
        with pytest.raises(ValueError, match="must be a string"):
            RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=(123,),  # type: ignore[arg-type]
                detail="ok",
            )

    def test_detail_rejects_overlong_string(self) -> None:
        """A 41-character detail string is rejected."""
        with pytest.raises(ValueError, match="at most 40 characters"):
            RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=(),
                detail="x" * 41,
            )

    def test_detail_rejects_non_string(self) -> None:
        """A non-string detail is rejected."""
        with pytest.raises(ValueError, match="must be a string"):
            RequiredChecksEvidence(
                boundary="nonpassing_check_run",
                contexts=(),
                detail=123,  # type: ignore[arg-type]
            )


class TestRequiredChecksGate:
    """Validation for the gate result dataclass."""

    def test_true_outcome_without_evidence_constructs(self) -> None:
        """True with no evidence is the success path."""
        gate = RequiredChecksGate(outcome=True, evidence=None)
        assert gate.outcome is True
        assert gate.evidence is None

    def test_false_outcome_with_evidence_constructs(self) -> None:
        """False may carry evidence."""
        ev = RequiredChecksEvidence(
            boundary="missing_requirement",
            contexts=(),
            detail="no required checks configured",
        )
        gate = RequiredChecksGate(outcome=False, evidence=ev)
        assert gate.outcome is False
        assert gate.evidence is ev

    def test_deferred_outcome_with_evidence_constructs(self) -> None:
        """Deferred outcomes may carry evidence."""
        ev = RequiredChecksEvidence(
            boundary="pending_check_run",
            contexts=("required-ci",),
            detail="status=in_progress",
        )
        gate = RequiredChecksGate(outcome=RequiredChecksDeferred.PENDING, evidence=ev)
        assert gate.outcome is RequiredChecksDeferred.PENDING
        assert gate.evidence is ev

    def test_true_outcome_rejects_evidence(self) -> None:
        """True with evidence is a contract violation."""
        ev = RequiredChecksEvidence(
            boundary="nonpassing_check_run",
            contexts=("required-ci",),
            detail="conclusion=failure",
        )
        with pytest.raises(ValueError, match="True outcome must not carry evidence"):
            RequiredChecksGate(outcome=True, evidence=ev)

    def test_outcome_rejects_invalid_type(self) -> None:
        """A non-bool, non-RequiredChecksDeferred outcome is rejected."""
        with pytest.raises(ValueError, match="outcome must be bool or RequiredChecksDeferred"):
            RequiredChecksGate(outcome="not_bool")  # type: ignore[arg-type]

    def test_frozen_mutation_rejected(self) -> None:
        """The dataclass is immutable."""
        gate = RequiredChecksGate(outcome=False, evidence=None)
        with pytest.raises(AttributeError):
            gate.outcome = True  # type: ignore[misc]
