"""Contracts for the repository ASD-STE100 writing policy."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

REQUIRED_POLICY_MARKERS = (
    "## ASD-STE100 writing standard",
    "ASD-STE100 Simplified Technical English",
    "Issue 9",
    "January 15, 2025",
    "MUST use",
    "https://www.asd-ste100.org/",
    "https://www.asd-ste100.org/STE_downloads.html",
    "project principles",
    "does not state or imply",
)


def test_canonical_agent_contract_requires_asd_ste100() -> None:
    """The authoritative contract keeps the complete mandatory policy."""
    contract = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")

    for marker in REQUIRED_POLICY_MARKERS:
        assert marker in contract
