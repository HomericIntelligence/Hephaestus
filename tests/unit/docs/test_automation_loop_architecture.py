"""Behavioral checks for links and source contracts in architecture docs."""

from __future__ import annotations

from pathlib import Path

from hephaestus.validation.doc_maintenance import (
    SOURCE_CONTRACTS,
    validate_source_contracts,
)
from hephaestus.validation.markdown import validate_file_links

REPO_ROOT = Path(__file__).resolve().parents[3]
ARCH_PATH = REPO_ROOT / "docs" / "architecture.md"
REVIEW_THREAD_ADR_PATH = REPO_ROOT / "docs" / "adr" / "0018-reviewer-owned-thread-reconciliation.md"
DEFAULT_BRANCH_ADR_PATH = (
    REPO_ROOT / "docs" / "adr" / "0052-verified-default-branch-merge-admission.md"
)


def test_architecture_links_to_its_live_routing_contract() -> None:
    """The architecture document remains linked to the executable route table."""
    routing_contract = next(
        contract
        for contract in SOURCE_CONTRACTS
        if contract.document == "docs/architecture.md" and contract.selector == "ROUTES"
    )

    assert validate_source_contracts(REPO_ROOT, contracts=(routing_contract,)) == []


def test_architecture_local_links_resolve() -> None:
    """Every local architecture-document link resolves in the repository."""
    result = validate_file_links(ARCH_PATH, REPO_ROOT)

    assert result["broken_links"] == []


def test_explicit_review_exception_is_documented_at_both_architecture_levels() -> None:
    """The architecture and ADR state the explicit broad-review exception."""
    architecture = ARCH_PATH.read_text()
    adr = REVIEW_THREAD_ADR_PATH.read_text()

    for document in (architecture, adr):
        assert "explicit operator" in document
        assert "inherited" in document
        assert "broad review" in document


def test_merge_wait_documents_verified_default_branch_admission() -> None:
    """Mutable architecture text and its ADR keep the fail-closed branch contract."""
    architecture = ARCH_PATH.read_text(encoding="utf-8")
    adr = DEFAULT_BRANCH_ADR_PATH.read_text(encoding="utf-8")

    for document in (architecture, adr):
        assert "verified" in document
        assert "repository default branch" in document
        assert "queue-driven merge requests" in document
        assert "Do not" in document
        assert "`main` fallback" in document or "guess `main`" in document
    assert "repository/default-branch/base/head" in architecture
    assert "open/`main`/unarmed/exclusive-GO" not in architecture
    assert "Verify --> Merge: matching reviewed head, main" not in architecture
