"""Regression tests for automation-loop architecture documentation."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from pathlib import Path

from hephaestus.automation.ci_fix_orchestrator import CIFixOrchestrator
from hephaestus.automation.prompts.implementation import (
    get_dirty_reused_worktree_decision_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
)
from hephaestus.automation.prompts.planning import (
    get_plan_loop_review_prompt,
    get_plan_prompt,
)

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md"
AGENTS_PATH = Path(__file__).resolve().parents[3] / "AGENTS.md"

PROMPT_REFS: tuple[tuple[str, str, Callable[..., object]], ...] = (
    ("prompts/planning.py", "get_plan_prompt", get_plan_prompt),
    ("prompts/planning.py", "get_plan_loop_review_prompt", get_plan_loop_review_prompt),
    (
        "prompts/implementation.py",
        "get_dirty_reused_worktree_decision_prompt",
        get_dirty_reused_worktree_decision_prompt,
    ),
    ("prompts/implementation.py", "get_implementation_prompt", get_implementation_prompt),
    (
        "prompts/implementation.py",
        "get_impl_resume_feedback_prompt",
        get_impl_resume_feedback_prompt,
    ),
    ("ci_fix_orchestrator.py", "build_ci_fix_prompt", CIFixOrchestrator.build_ci_fix_prompt),
    (
        "ci_fix_orchestrator.py",
        "force_engagement_prompt",
        CIFixOrchestrator.force_engagement_prompt,
    ),
)


def _arch_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_ci_stage_documents_shipped_classifier() -> None:
    """The CI-stage doc must describe classify_ci_state as shipped code."""
    text = DOC_PATH.read_text(encoding="utf-8")

    assert "classify_ci_state" in text
    assert "NEW pure function" not in text
    assert "does not exist yet" not in text
    assert "shipped pure classifier" in text
    assert "tests/unit/automation/pipeline/stages/test_classify_ci_state.py" in text


def test_issue_1929_prompt_line_refs_match_source_definitions() -> None:
    """Documented prompt-function line references stay aligned with source."""
    doc = DOC_PATH.read_text(encoding="utf-8")

    for path, function_name, function in PROMPT_REFS:
        expected_line = str(inspect.getsourcelines(function)[1])
        refs = re.findall(rf"`{re.escape(path)}:(\d+)\s+{function_name}`", doc)
        assert refs, f"missing documented reference for {path} {function_name}"
        assert set(refs) == {expected_line}


def test_automation_loop_architecture_status_is_implemented() -> None:
    """The architecture contract must describe the implemented pipeline."""
    text = _arch_text()
    header = "\n".join(text.splitlines()[:5])

    assert "Status: implemented for the epic #1809 queue-based automation loop." in header
    assert "pre-implementation" not in header


def test_automation_loop_architecture_describes_post_cutover_loop() -> None:
    """The architecture contract must not document removed rollback controls."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "subprocess-per-phase loop was removed" in text
    assert "there is no `--pipeline` compatibility flag" in normalized
    assert "there is no `--legacy-loop` rollback path" in normalized
    assert "hephaestus-automation-loop --pipeline" not in text
    assert "`--pipeline` remains accepted" not in text
    assert "legacy loop remains available" not in normalized
    assert "HEPH_PIPELINE=0" not in text
    assert "forces the pre-pipeline path" not in text


def test_automation_loop_architecture_describes_thin_queue_wrappers() -> None:
    """Wrapper docs must describe queue-pipeline scoped entry points, not legacy paths."""
    text = _arch_text()

    assert "thin queue-pipeline scoped entry points" in text
    assert "standalone console scripts remain legacy/manual compatibility paths" not in text
    assert "still invokes the legacy planner entry point" not in text
    assert "still invokes the legacy implementer entry" not in text


def test_agents_map_describes_thin_queue_wrappers() -> None:
    """The root agent map must not carry stale cutover-era wrapper language."""
    text = AGENTS_PATH.read_text(encoding="utf-8")

    assert "thin queue-pipeline scoped entry points" in text
    assert "rollback or out-of-band entry points" not in text
    assert "during the #1818 cutover" not in text


def test_automation_loop_architecture_has_interrupt_semantics_and_exit_codes() -> None:
    """The architecture contract must keep interrupt and exit-code details."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "Finalized in the cutover issue." not in text
    assert "## Interrupt semantics and exit codes" in text
    assert "SIGINT, SIGTERM, and SIGHUP" in text
    assert "resumable at <stage>" in text
    assert "Exit codes are stable: `130` for interrupted runs" in text
    assert (
        "If an interrupt overlaps a non-passing ledger entry or fatal coordinator error, "
        "`130` deliberately takes priority because the run did not complete." in normalized
    )


def test_automation_loop_architecture_has_concurrency_cli_dry_run_and_glossary() -> None:
    """The architecture contract keeps concurrency, CLI, dry-run, and glossary details."""
    text = _arch_text()

    assert "## Concurrency and tuning" in text
    assert "`parallel_repos * max_workers`" in text
    assert "`--phase-timeout` bounds each agent job" in text
    assert "Dry-run mode logs GitHub mutations and job submissions without executing them" in text
    assert "## CLI scopes and rollout controls" in text
    assert "## Glossary" in text
    assert "- **Coordinator**:" in text
