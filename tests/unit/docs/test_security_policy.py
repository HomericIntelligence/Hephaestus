"""Regression tests for the SECURITY.md disclosure safe-harbor guidance."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SECURITY_DOC = REPO_ROOT / "SECURITY.md"


def _policy_text() -> str:
    return SECURITY_DOC.read_text(encoding="utf-8")


def test_safe_harbor_section_exists() -> None:
    """The policy must carry an explicit safe-harbor section."""
    assert re.search(r"^### Safe Harbor & Scope Eligibility\s*$", _policy_text(), re.MULTILINE)


def test_safe_harbor_states_no_legal_action_commitment() -> None:
    """Good-faith research must be met with a no-legal-action commitment."""
    text = _policy_text().lower()
    assert "good faith" in text
    assert "will not pursue or support legal action" in text


def test_safe_harbor_enumerates_ineligible_activities() -> None:
    """Scope eligibility must name the activities that void safe harbor."""
    text = _policy_text().lower()
    for excluded in ("social engineering", "denial of service", "third-party"):
        assert excluded in text, f"safe-harbor exclusions must mention {excluded!r}"


def test_remediation_handling_section_exists() -> None:
    """The policy must carry an explicit remediation-handling section."""
    assert re.search(r"^### Remediation Handling\s*$", _policy_text(), re.MULTILINE)


def test_remediation_targets_cover_reporting_severity_scale() -> None:
    """Every severity the reporting section asks for has a remediation target."""
    match = re.search(
        r"^### Remediation Handling\s*$(.*?)(?=^##\s|\Z)",
        _policy_text(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    section = match.group(1)
    for severity in ("Critical", "High", "Medium", "Low"):
        assert severity in section, f"missing remediation target for {severity}"


def test_remediation_defines_disclosure_window() -> None:
    """A coordinated-disclosure window must be spelled out."""
    text = _policy_text()
    assert "90 days" in text


def test_no_hardcoded_date_stamps() -> None:
    """New sections must not reintroduce 'As of YYYY-MM-DD' stamps (pre-commit hook)."""
    assert not re.search(r"As of \d{4}-\d{2}-\d{2}", _policy_text())
