"""Regression tests for the backup-and-disaster-recovery runbook.

Guards the operator-facing DR procedure against silent drift: the runbook and
its ADR-0013 policy link, the tier table, and the fail-closed restore semantics
must survive edits. Modeled on ``test_automation_loop_crash_runbook.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "backup-restore.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"

_SCOPE_SECTION_RE = re.compile(
    r"^##\s+Scope\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _runbook_text() -> str:
    """Return the runbook contents, normalized to lowercase collapsed whitespace."""
    return re.sub(r"\s+", " ", RUNBOOK.read_text(encoding="utf-8").lower())


def _scope_section() -> str:
    """Return the Scope section, normalized to lowercase collapsed whitespace."""
    text = RUNBOOK.read_text(encoding="utf-8")
    match = _SCOPE_SECTION_RE.search(text)
    assert match is not None, "backup-restore.md must have a '## Scope' section."
    return re.sub(r"\s+", " ", match.group(1).lower())


def test_runbook_exists() -> None:
    """The DR runbook file exists."""
    assert RUNBOOK.is_file(), "docs/runbooks/backup-restore.md must exist."


def test_runbook_has_required_sections() -> None:
    """The runbook documents each operator procedure section."""
    text = RUNBOOK.read_text(encoding="utf-8")
    for section in (
        "## Scope",
        "## Taking a backup",
        "## Restoring state",
        "## Full workstation loss",
        "## Verification drill",
        "## What is never backed up",
    ):
        assert section in text, f"backup-restore.md missing section {section!r}"


def test_scope_section_names_state_and_policy() -> None:
    """The Scope section ties the runbook to the tiered state and ADR-0013 policy."""
    scope = _scope_section()
    assert "build/.issue_implementer" in scope
    assert "uv.lock" in scope
    assert "adr-0013" in scope


def test_runbook_documents_fail_closed_and_credentials_rule() -> None:
    """Restore fail-closed behavior and the never-archive-credentials rule are stated."""
    text = _runbook_text()
    assert "fail-closed" in text or "fail closed" in text
    assert "--force" in text
    assert "path-traversal" in text or "path traversal" in text
    assert "never" in text and "credentials" in text
    # The tested-restore claim points at the CI test that executes it.
    assert "tests/unit/scripts/test_backup_state.py" in text


def test_index_links_the_runbook() -> None:
    """The runbook index links backup-restore.md."""
    index = RUNBOOK_INDEX.read_text(encoding="utf-8")
    assert "(backup-restore.md)" in index
