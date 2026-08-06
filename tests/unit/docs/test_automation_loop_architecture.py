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
