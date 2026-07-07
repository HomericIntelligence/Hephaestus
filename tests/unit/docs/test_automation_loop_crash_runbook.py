"""Regression tests for the automation-loop crash runbook."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "automation-loop-crash.md"

_PIPELINE_SECTION_RE = re.compile(
    r"^##\s+Pipeline recovery semantics\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _pipeline_section() -> str:
    """Return the pipeline recovery section from the crash runbook."""
    text = RUNBOOK.read_text(encoding="utf-8")
    match = _PIPELINE_SECTION_RE.search(text)
    assert match is not None, (
        "automation-loop-crash.md must document pipeline recovery semantics "
        "for interrupted or crashed --pipeline runs."
    )
    return re.sub(r"\s+", " ", match.group(1).lower())


def test_pipeline_recovery_semantics_are_documented() -> None:
    """The crash runbook must cover pipeline interrupt and restart behavior."""
    section = _pipeline_section()

    assert "--pipeline" in section
    assert "exit code 130" in section
    assert "resumable" in section
    assert "never failed" in section
    assert "30s" in section
    assert "second signal" in section
    assert "github labels" in section
    assert "pr state" in section
    assert "local worktrees" in section
    assert "no persisted queue snapshot" in section
