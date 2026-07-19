"""Regression tests for automation-loop architecture documentation (canonical).

The canonical source is `docs/architecture.md`; these tests assert
against it directly.
"""

from __future__ import annotations

from pathlib import Path

# Canonical sources.
ARCH_PATH = Path(__file__).resolve().parents[3] / "docs" / "architecture.md"
AGENTS_PATH = Path(__file__).resolve().parents[3] / "AGENTS.md"
COORDINATOR_PATH = (
    Path(__file__).resolve().parents[3]
    / "hephaestus"
    / "automation"
    / "pipeline"
    / "coordinator.py"
)


def _arch_text() -> str:
    return ARCH_PATH.read_text(encoding="utf-8")


def test_architecture_md_preserves_interrupt_semantics_and_exit_codes_section_12() -> None:
    """Section 12 'Interrupt semantics and exit codes' must remain in the canonical doc."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "## 12. Interrupt semantics and exit codes" in text
    assert "SIGINT, SIGTERM, SIGHUP" in text
    assert "resumable at <stage>" in text
    # Exit-code precedence: 130 (interrupted) wins over non-passing ledger.
    assert "130" in text and "priority" in normalized and "interrupted" in normalized


def test_architecture_md_documents_thin_cli_scope_wrappers_section_9() -> None:
    """Section 9 'Thin CLI scope wrappers and rollout controls' must remain."""
    text = _arch_text()

    assert "## 9. Thin CLI scope wrappers and rollout controls" in text
    assert "thin queue-pipeline scoped entry points" in text
    # Must NOT carry legacy standalone-script language.
    assert "standalone console scripts remain legacy" not in text
    assert "still invokes the legacy planner entry point" not in text
    assert "still invokes the legacy implementer entry" not in text


def test_architecture_md_documents_concurrency_pool_size_section_2() -> None:
    """Section 2 carries the canonical `Pool size` line.

    The pool-size invariant crosses into section 8's worker-pool contract.
    """
    text = _arch_text()

    assert "## 8. The worker pool and job contract" in text
    # Pool size uses the unicode x separator per docs/architecture.md.
    assert "Pool size = `parallel_repos × max_workers`" in text


def test_architecture_md_has_glossary_section_13() -> None:
    """Section 13 'Glossary' must remain."""
    text = _arch_text()

    assert "## 13. Glossary" in text
    # Coordinator is one of the glossary terms (bolded).
    assert "**Coordinator**" in text or "**WorkItem**" in text


def test_architecture_md_documents_dry_run() -> None:
    """Dry-run semantics are documented (typically in section 10 Observability)."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "Dry-run" in text or "dry-run" in text
    assert "would-submit" in normalized or "ADVANCE" in text


def test_architecture_md_no_legacy_pipeline_flag_text() -> None:
    """The canonical doc must not carry legacy `--pipeline` flag language."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "subprocess-per-phase loop was removed" not in text
    assert "there is no `--pipeline` compatibility flag" not in normalized
    assert "there is no `--legacy-loop` rollback path" not in normalized
    assert "hephaestus-automation-loop --pipeline" not in text
    assert "`--pipeline` remains accepted" not in text
    assert "legacy loop remains available" not in normalized
    assert "HEPH_PIPELINE=0" not in text
    assert "forces the pre-pipeline path" not in text


def test_architecture_md_is_first_iteration_without_changelog() -> None:
    """First iteration: the canonical doc carries no changelog or revision tracking.

    Banned tokens:
      - `In-flight refactor notice` banner
      - `in-flight-architectural-prs.md` cross-link (the pointer file is gone)
      - `Delete-on-rewrite trigger` blockquote
      - `Coordinated PR sequencing` heading
      - `removed by PR #2280; see §15` / `Planned change (PR #2280, in flight)` markers
      - The §15 / §16 numbered sections
    """
    text = _arch_text()

    assert "In-flight refactor notice" not in text
    assert "in-flight-architectural-prs.md" not in text
    assert "Delete-on-rewrite trigger" not in text
    assert "Coordinated PR sequencing" not in text
    assert "Planned change (PR #2280, in flight)" not in text
    assert "removed by PR #2280; see §15" not in text
    assert "## 15. In-flight" not in text
    assert "## 16. Other in-flight" not in text


def test_in_flight_pointer_file_is_removed() -> None:
    """The in-flight tracking file is removed in this first-iteration cleanup."""
    in_flight = Path(__file__).resolve().parents[3] / "docs" / "in-flight-architectural-prs.md"
    assert not in_flight.exists(), (
        f"expected {in_flight} to be deleted; the first iteration carries no in-flight tracking"
    )


def test_agents_map_describes_thin_queue_wrappers() -> None:
    """The root agent map must not carry stale cutover-era wrapper language."""
    text = AGENTS_PATH.read_text(encoding="utf-8")

    assert "thin queue-pipeline scoped entry points" in text
    assert "rollback or out-of-band entry points" not in text
    assert "during the #1818 cutover" not in text


def test_coordinator_no_pipeline_selector_flags() -> None:
    """Coordinator comments must not describe removed pipeline selector flags."""
    text = COORDINATOR_PATH.read_text(encoding="utf-8")

    assert "when ``--pipeline`` is passed explicitly" not in text
    assert "Under ``--pipeline``" not in text
    assert "legacy path binds it to the phase subprocess" not in text


def test_architecture_md_documents_effective_item_semantics() -> None:
    """The architecture contract documents terminalization and summary collapse."""
    text = _arch_text()
    normalized = " ".join(text.split())

    assert "merged/closed" in text
    assert "Effective-item rule" in text
    assert "latest_logical_items" in text
    # exit-code coverage: relies on the 3 stable assertions above + normalized.
    assert "exit-code" in normalized


def test_stage_github_pr_state_docstring_describes_shared_read_contract() -> None:
    """The shared PR-state accessor must describe every pipeline consumer."""
    from hephaestus.automation.pipeline.stages.base import StageGitHub

    docstring = StageGitHub.gh_pr_state.__doc__ or ""
    normalized = " ".join(docstring.split())

    assert "repo" in normalized
    assert "implementation" in normalized
    assert "merge_wait" in normalized
    assert "terminal-state" in normalized or "terminal state" in normalized
