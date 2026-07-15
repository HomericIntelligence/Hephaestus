"""Regression tests for maintained normative documentation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

NORMATIVE_DOCS = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "COMPATIBILITY.md",
    "CONTRIBUTING.md",
    "docs/DEFINITION_OF_DONE.md",
    "docs/AUTOMATION_LOOP_ARCHITECTURE.md",
    "docs/ci/required-checks.md",
    "docs/MIGRATION.md",
    "docs/ROADMAP.md",
    "docs/runbooks/index.md",
    "docs/runbooks/ci-driver-stall.md",
)

FORBIDDEN_SNAPSHOT_PATTERNS = (
    re.compile(r"\bas of 20\d{2}-\d{2}-\d{2}\b", re.IGNORECASE),
    re.compile(r"\bLast updated:\s*20\d{2}-\d{2}-\d{2}\b", re.IGNORECASE),
    re.compile(r"\bCurrent Focus \(Q[1-4] 20\d{2}\)", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?k\s+LoC\b", re.IGNORECASE),
    re.compile(r"\b\d+(?:\.\d+)?%\s+of the codebase\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+documented subpackages\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+(?:SKILL\.md\s+)?skills\b", re.IGNORECASE),
    re.compile(r"\b\d[\d,]*\+\s+tests across\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+of\s+\d+\s+declared tools\b", re.IGNORECASE),
    re.compile(r"\b\d+\s+excluded automation modules\b", re.IGNORECASE),
)


@pytest.mark.parametrize("relative_path", NORMATIVE_DOCS)
def test_normative_docs_have_no_unowned_snapshots(relative_path: str) -> None:
    """Reject calendar, source-size, and unvalidated inventory snapshots."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    matches = [pattern.pattern for pattern in FORBIDDEN_SNAPSHOT_PATTERNS if pattern.search(text)]
    assert not matches, f"{relative_path} contains unowned snapshots: {matches}"


@pytest.mark.parametrize("relative_path", NORMATIVE_DOCS)
def test_completed_rollout_ids_are_not_normative_state(relative_path: str) -> None:
    """Keep temporary rollout issue state out of normative documents."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "#2054" not in text
    assert "#2055" not in text


def test_unvalidated_console_script_count_is_not_duplicated() -> None:
    """Keep the console-script inventory in its executable source."""
    text = (REPO_ROOT / "COMPATIBILITY.md").read_text(encoding="utf-8")
    assert re.search(r"\b\d+\s+console scripts\b", text) is None


def test_contributor_quick_start_defers_merge_policy_to_the_runbook() -> None:
    """Keep the contributor quick-start free of duplicated merge policy."""
    text = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    quick_start = text.split("## Your first day", maxsplit=1)[1].split(
        "## Planning artifacts", maxsplit=1
    )[0]

    assert "docs/ci/required-checks.md" in quick_start
    for marker in (
        "keep auto-merge disabled",
        "unconditional independent strict-review GO",
        "manual squash merge",
    ):
        assert marker not in quick_start


def test_plugin_owned_skill_catalog_uses_settings_source() -> None:
    """Keep plugin enablement tied to its settings source, not prose copies."""
    for relative_path in ("AGENTS.md", "CLAUDE.md"):
        text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
        assert ".claude/settings.json" in text
        assert "(skills/" not in text
        assert "`skills/" not in text


@pytest.mark.parametrize(
    "relative_path",
    (
        "docs/AUTOMATION_LOOP_ARCHITECTURE.md",
        "docs/ci/required-checks.md",
    ),
)
def test_operational_sources_declare_maintenance(relative_path: str) -> None:
    """Require ownership and update criteria for operational documentation."""
    text = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for marker in (
        "## Maintenance",
        "**Owner:**",
        "**Maintained sources:**",
        "**Update trigger:**",
    ):
        assert marker in text, f"{relative_path} is missing {marker}"


def test_documentation_index_names_maintained_sources() -> None:
    """Make the documentation index point readers to authoritative sources."""
    text = (REPO_ROOT / "docs" / "index.md").read_text(encoding="utf-8")
    for source in (
        "`pyproject.toml`",
        "`.claude/settings.json`",
        "`docs/AUTOMATION_LOOP_ARCHITECTURE.md`",
        "`docs/ci/required-checks.md`",
        "`docs/ROADMAP.md`",
        "`docs/MIGRATION.md`",
    ):
        assert source in text
