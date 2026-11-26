"""Tests for the publication and repository views of the spec source."""

from __future__ import annotations

from hephaestus.git.verification.publication_spec import (
    FIRST_PUBLICATION_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S,
)
from hephaestus.git.verification.repository import (
    REBASE_STRUCTURAL_TEST_ARGV,
    REBASE_STRUCTURAL_TEST_PATH,
    is_hephaestus_repository,
)
from hephaestus.git.verification.specs import (
    FIRST_PUBLICATION_CHECK,
    HEPHAESTUS_REQUIRED_CHECK,
    REBASE_STRUCTURAL_TEST,
)


def test_publication_view_shares_the_canonical_argv() -> None:
    """Publication consumers see the one canonical command source."""
    assert HEPHAESTUS_REQUIRED_CHECK_ARGV is HEPHAESTUS_REQUIRED_CHECK.argv
    assert FIRST_PUBLICATION_CHECK_ARGV is FIRST_PUBLICATION_CHECK.argv
    assert HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S == 7200


def test_repository_view_shares_the_canonical_rebase_spec() -> None:
    """The repository policy sees the one canonical rebase command."""
    assert REBASE_STRUCTURAL_TEST_ARGV is REBASE_STRUCTURAL_TEST.argv
    assert REBASE_STRUCTURAL_TEST_PATH in REBASE_STRUCTURAL_TEST_ARGV


def test_repository_identity_match_is_case_insensitive() -> None:
    """Only the Hephaestus identity selects the Hephaestus policy."""
    assert is_hephaestus_repository("homericintelligence/hephaestus") is True
    assert is_hephaestus_repository("HomericIntelligence/Odyssey") is False
