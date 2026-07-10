"""Regression tests for the required-checks protection runbook."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
POLICY_DOC = REPO_ROOT / "docs" / "ci" / "required-checks.md"

_REAPPLY_SECTION_RE = re.compile(
    r"^## \(Re-\)applying branch protection\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _policy_text() -> str:
    return POLICY_DOC.read_text(encoding="utf-8")


def _reapply_section() -> str:
    match = _REAPPLY_SECTION_RE.search(_policy_text())
    assert match is not None, "required-checks.md must contain the reapply runbook"
    return match.group(1)


def test_strict_policy_has_operational_reason() -> None:
    """Require the documented strict policy to explain its purpose."""
    text = _policy_text().lower()

    assert "strict: true" in text
    assert "stale pr" in text
    assert "newer `main`" in text


def test_repair_patches_only_strict_mode() -> None:
    """Require the runbook to patch only the classic strict flag."""
    section = _reapply_section().lower()

    assert "-x patch" in section
    assert "-f strict=true" in section
    assert "-x put" not in section
    assert "checks[][context]" not in section


def test_runbook_audits_rulesets_and_preserves_bindings() -> None:
    """Require ruleset inventory and check-binding preservation safeguards."""
    section = _reapply_section()

    required_markers = (
        'state_dir=$(mktemp -d "${TMPDIR:-/tmp}/projecthephaestus-issue-2025.XXXXXX")',
        "rulesets?includes_parents=true&targets=branch",
        "rules/branches/$branch",
        "gh ruleset check --default",
        "app_id",
        "integration_id",
        '($status_rules | length) > 0',
        'has(\"context\") and has(\"integration_id\")',
        "cmp -s",
    )
    for marker in required_markers:
        assert marker in section, f"required-checks runbook is missing {marker!r}"
