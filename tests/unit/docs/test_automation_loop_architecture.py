"""Regression tests for automation-loop architecture documentation."""

from __future__ import annotations

from pathlib import Path

DOC_PATH = Path(__file__).resolve().parents[3] / "docs" / "AUTOMATION_LOOP_ARCHITECTURE.md"
AGENTS_PATH = Path(__file__).resolve().parents[3] / "AGENTS.md"
COORDINATOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "hephaestus"
    / "automation"
    / "pipeline"
    / "coordinator.py"
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


def test_automation_loop_architecture_describes_the_shipped_pipeline() -> None:
    """The architecture contract must describe the pipeline as shipped."""
    text = _arch_text()
    header = "\n".join(text.splitlines()[:8])

    assert "The `hephaestus-automation-loop` CLI runs this pipeline directly" in header
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


def test_coordinator_comments_describe_queue_only_loop() -> None:
    """Coordinator comments must not describe removed pipeline selector flags."""
    text = COORDINATOR_PATH.read_text(encoding="utf-8")

    assert "when ``--pipeline`` is passed explicitly" not in text
    assert "Under ``--pipeline``" not in text
    assert "legacy path binds it to the phase subprocess" not in text


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


def test_automation_loop_architecture_documents_effective_item_semantics() -> None:
    """The architecture contract documents terminalization and summary collapse."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "merged or closed" in normalized
    assert "before branch adoption" in normalized
    assert "latest effective logical item" in normalized
    assert "Summary rows" in normalized
    assert "preserved worktree guidance" in normalized
    assert "exit-code calculation" in normalized


def test_stage_github_pr_state_docstring_describes_shared_read_contract() -> None:
    """The shared PR-state accessor must describe every pipeline consumer."""
    from hephaestus.automation.pipeline.stages.base import StageGitHub

    docstring = StageGitHub.gh_pr_state.__doc__ or ""
    normalized = " ".join(docstring.split())

    assert "repo" in normalized
    assert "CI" in normalized
    assert "implementation" in normalized
    assert "merge_wait" in normalized
    assert "terminal-state checks" in normalized


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
