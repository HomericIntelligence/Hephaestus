"""Regression tests for the required-checks protection runbook."""

from __future__ import annotations

import json
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


def _runbook_contexts(variable: str) -> list[str]:
    """Return one single-quoted JSON context array from the policy runbook."""
    match = re.search(
        rf"^{re.escape(variable)}='(?P<contexts>\[.*?\])'$",
        _reapply_section(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"required-checks runbook is missing {variable}"
    contexts = json.loads(match.group("contexts"))
    assert isinstance(contexts, list)
    assert all(isinstance(context, str) for context in contexts)
    return contexts


def test_strict_policy_has_operational_reason() -> None:
    """Require the documented strict policy to explain its purpose.

    The repo runs ``strict: false`` deliberately (see the doc): required checks
    still gate merges, but PRs are NOT required to be up to date with ``main``,
    which avoids the rebase churn a fast-moving ``main`` would otherwise cause.
    """
    text = _policy_text().lower()

    assert "strict: false" in text
    assert "churn" in text
    assert "up to date" in text


def test_policy_documents_live_dual_enforcement() -> None:
    """The policy names both classic and ruleset required-check surfaces."""
    text = _policy_text()

    for context in (
        "`required-checks-gate`",
        "`test (ubuntu-latest, 3.12, unit)`",
        "`test (ubuntu-latest, 3.12, integration)`",
        "`strict-review-proof`",
    ):
        assert context in text
    for context in (
        "`lint`",
        "`unit-tests`",
        "`integration-tests`",
        "`security/dependency-scan`",
        "`security/secrets-scan`",
        "`build`",
        "`schema-validation`",
        "`deps/version-sync`",
        "`pr-policy`",
    ):
        assert context in text
    assert "classic branch-protection context" in text
    assert "direct ruleset context" in text


def test_repair_patches_only_strict_mode() -> None:
    """Require the runbook to patch only the classic strict flag."""
    section = _reapply_section().lower()

    assert "-x patch" in section
    assert "-f strict=false" in section
    assert "-x put" not in section
    assert "checks[][context]" not in section


def test_runbook_audits_rulesets_and_preserves_bindings() -> None:
    """Require ruleset inventory and check-binding preservation safeguards."""
    section = _reapply_section()

    required_markers = (
        'state_dir=$(mktemp -d "${TMPDIR:-/tmp}/hephaestus-issue-2025.XXXXXX")',
        "rulesets?includes_parents=true&targets=branch",
        "rules/branches/$branch",
        "gh ruleset check --default",
        "app_id",
        "integration_id",
        "expected_classic",
        "expected_ruleset",
        "($status_rules | length) == 1",
        "map(.context) | sort",
        "$expected_ruleset | sort",
        'has("context") and has("integration_id")',
        "cmp -s",
    )
    for marker in required_markers:
        assert marker in section, f"required-checks runbook is missing {marker!r}"


def test_runbook_validates_exact_live_context_inventories() -> None:
    """The runbook's assertions cannot drift from the documented live contract."""
    assert _runbook_contexts("expected_classic") == [
        "required-checks-gate",
        "test (ubuntu-latest, 3.12, unit)",
        "test (ubuntu-latest, 3.12, integration)",
        "strict-review-proof",
    ]
    assert _runbook_contexts("expected_ruleset") == [
        "lint",
        "unit-tests",
        "integration-tests",
        "security/dependency-scan",
        "security/secrets-scan",
        "build",
        "schema-validation",
        "deps/version-sync",
        "pr-policy",
    ]


def test_successor_adr_records_dual_required_check_surfaces() -> None:
    """The live dual-surface contract supersedes, but does not rewrite, ADR-0004."""
    successor = REPO_ROOT / "docs" / "adr" / "0007-dual-surface-required-checks.md"

    assert successor.is_file(), "dual-surface required-checks successor ADR is missing"
    text = successor.read_text(encoding="utf-8")
    assert "Supersedes: ADR-0004" in text
    assert "required-checks-gate" in text
    assert "classic branch protection" in text
    assert "direct ruleset" in text
