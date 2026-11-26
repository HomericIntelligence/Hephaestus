"""Tests for the single canonical host-verification spec source."""

from __future__ import annotations

import pytest

from hephaestus.git.verification.specs import (
    FIRST_PUBLICATION_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK_ARGV,
    HOST_VERIFICATION_SPECS,
    PRE_PR_TEST_ARGV,
    REBASE_STRUCTURAL_TEST_ARGV,
    HostVerificationSpec,
    focused_test_argv,
    spec_for,
)


def test_spec_table_has_one_entry_per_identifier() -> None:
    """Each canonical check owns exactly one stable identifier."""
    descriptions = [spec.descr for spec in HOST_VERIFICATION_SPECS]

    assert len(descriptions) == len(set(descriptions))
    assert len(descriptions) == 4


def test_argv_constants_match_the_canonical_specs() -> None:
    """Exported argv values stay identical to the historical fixed commands."""
    assert PRE_PR_TEST_ARGV == ("uv", "run", "pytest", "tests", "-q", "--tb=short")
    assert HEPHAESTUS_REQUIRED_CHECK_ARGV == (
        "bash",
        "scripts/run_ci_local.sh",
        "all",
        "--rebuild",
    )
    assert FIRST_PUBLICATION_CHECK_ARGV == (
        "bash",
        "scripts/run_ci_local.sh",
        "all",
        "--check-only",
    )
    assert REBASE_STRUCTURAL_TEST_ARGV[:3] == ("uv", "run", "pytest")
    assert "-o" in REBASE_STRUCTURAL_TEST_ARGV


def test_spec_for_returns_only_an_exact_identifier() -> None:
    """Lookup selects the spec by its exact description."""
    spec = spec_for("implementation_pre_pr_test")

    assert spec is not None
    assert spec.argv == PRE_PR_TEST_ARGV
    assert spec_for("implementation") is None


def test_focused_test_argv_uses_one_fixed_command_shape() -> None:
    """A trusted test path yields a bounded pytest command."""
    path = "tests/unit/git/verification/test_specs.py"

    assert focused_test_argv(path) == (
        "uv",
        "run",
        "pytest",
        "-o",
        "addopts=",
        path,
        "-q",
        "--tb=short",
    )
    with pytest.raises(ValueError, match="repository-relative"):
        focused_test_argv("../outside.py")


def test_spec_rejects_invalid_command_vectors() -> None:
    """A spec cannot hold an empty command or an escaping changed path."""
    with pytest.raises(ValueError, match="argv"):
        HostVerificationSpec("broken", ())
    with pytest.raises(ValueError, match="changed path"):
        HostVerificationSpec("broken", ("uv", "run"), changed_path="../outside.py")
