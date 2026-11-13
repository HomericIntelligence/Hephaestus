"""Documentation assertions for the automation-loop decomposition design."""

from __future__ import annotations

from pathlib import Path

import hephaestus

_DOCS = Path(hephaestus.__file__).parent.parent / "docs"
_ARCHITECTURE = _DOCS / "architecture.md"


def _architecture_text() -> str:
    """Return the architecture document text."""
    return _ARCHITECTURE.read_text(encoding="utf-8")


def test_plan_review_documents_the_decomposition_triggers() -> None:
    """The plan-review section names both durable decomposition triggers."""
    text = _architecture_text()
    assert "Plan decomposition triggers (issue #2982)" in text
    assert "Lossless size" in text
    assert "Convergence" in text
    assert "MAX_CURRENT_REVISION_CONTEXT_CHARS" in text
    assert "plan_nogo_count" in text


def test_plan_review_documents_the_durable_decomposition_transaction() -> None:
    """The architecture names every host-supervised transaction step."""
    text = _architecture_text()
    assert "Plan decomposition transaction (issue #2982)" in text
    for step in (
        "Generation",
        "Independent review",
        "Child creation and readback",
        "Parent transition",
        "Admission",
    ):
        assert step in text
    assert "draft digest" in text
    assert "recovery marker" in text
    assert "state:needs-plan" in text


def test_plan_review_documents_the_same_run_recursion_guard() -> None:
    """A generated child defers to a later run instead of recursing."""
    text = _architecture_text()
    assert "recursion guard" in text
    assert "defers a generated child issue" in text
    assert "later run" in text


def test_plan_review_documents_the_topological_child_admission() -> None:
    """Children queue one topological layer at a time after verified records."""
    text = _architecture_text()
    assert "one topological layer at a time" in text
    assert "direct prerequisites finished successfully" in text
    assert "finishes every not-yet-queued descendant failed" in text


def test_plan_review_documents_the_dry_run_and_fail_closed_behavior() -> None:
    """Dry-run publishes nothing, and partial trees are never admitted."""
    text = _architecture_text()
    assert "dry-run publishes no decomposition mutation" in text
    assert "queues no" in text
    assert "a partial tree is never" in text
    assert "ADR-0066" in text


def test_architecture_doc_links_resolve() -> None:
    """Every relative link target named in the decomposition sections exists."""
    for target in (
        "../hephaestus/automation/review_journal.py",
        "../hephaestus/automation/pipeline/plan_journal.py",
        "../hephaestus/automation/plan_decomposition.py",
    ):
        assert (_DOCS / target).resolve().exists(), target
    assert (_DOCS / "adr" / "0066-plan-decomposition.md").exists()
