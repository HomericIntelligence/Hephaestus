"""Guards for the implementation-state label and merge-wait authority contract."""

from __future__ import annotations

from pathlib import Path

from hephaestus.automation.state_labels import STATE_IMPLEMENTATION_GO, STATE_LABEL_SPECS

REPO_ROOT = Path(__file__).resolve().parents[3]
ADR = REPO_ROOT / "docs" / "adr" / "0014-confirmed-implementation-state-labels.md"
ADR_0012 = REPO_ROOT / "docs" / "adr" / "0012-loop-owned-pr-review-approval.md"
ADR_INDEX = REPO_ROOT / "docs" / "adr" / "README.md"
ARCHITECTURE = REPO_ROOT / "docs" / "architecture.md"
AGENTS = REPO_ROOT / "AGENTS.md"
REQUIRED_CHECKS = REPO_ROOT / "docs" / "ci" / "required-checks.md"
GITHUB_README = REPO_ROOT / ".github" / "README.md"
RUNBOOK_INDEX = REPO_ROOT / "docs" / "runbooks" / "index.md"
DRIVE_GREEN_RUNBOOK = REPO_ROOT / "docs" / "runbooks" / "ci-driver-stall.md"
DEFINITION_OF_DONE = REPO_ROOT / "docs" / "DEFINITION_OF_DONE.md"


def _text(path: Path) -> str:
    """Read a repository text file."""
    return path.read_text(encoding="utf-8")


def _normalized(path: Path) -> str:
    """Read a repository text file with collapsed whitespace."""
    return " ".join(_text(path).split())


def test_adr_0014_supersedes_adr_0012_active_contract() -> None:
    """The active approval contract is recorded as a superseding ADR."""
    text = _text(ADR)
    normalized = _normalized(ADR)
    index = _text(ADR_INDEX)

    assert "- Supersedes: ADR-0012" in text
    assert "confirmed exclusive" in text
    assert "not standalone merge authorization" in text
    assert "No queue stage creates, disables, adopts, or polls an auto-merge request" in normalized
    assert "0014-confirmed-implementation-state-labels.md" in index
    assert "0012-loop-owned-pr-review-approval.md" in index
    assert "superseded by 0014" in index
    assert "superseded by ADR-0014" in _text(ADR_0012)


def test_architecture_documents_confirmed_labels_and_process_local_proof() -> None:
    """Architecture must match implementation-state label read-back semantics."""
    normalized = _normalized(ARCHITECTURE)

    assert "ADR-0014" in normalized
    assert "Implementation-state labels are confirmed exclusive labels" in normalized
    assert "not standalone merge authorization" in normalized
    assert (
        "confirmed `state:implementation-go` label and a matching in-memory reviewed-head proof"
        in normalized
    )
    assert "confirmed by read-back" in normalized


def test_operator_docs_do_not_treat_go_label_as_merge_authority() -> None:
    """Operator-facing docs must preserve the label-plus-proof standby contract."""
    combined = "\n".join(
        _text(path)
        for path in (
            AGENTS,
            REQUIRED_CHECKS,
            GITHUB_README,
            RUNBOOK_INDEX,
            DRIVE_GREEN_RUNBOOK,
            DEFINITION_OF_DONE,
        )
    )
    normalized = " ".join(combined.split())

    assert "ADR-0014" in normalized
    assert "process-local reviewed-head proof" in normalized
    assert "confirmed `state:implementation-go` label" in normalized
    assert "reaches merge-wait standby rather than automatic merge" in normalized
    assert "conditionally arms in `merge_wait`" not in combined
    assert "authorizes the loop's merge-wait step" not in combined
    assert "merge arming" not in combined


def test_state_label_description_keeps_merge_wait_proof_requirement() -> None:
    """The provisioning label description must not imply label-only approval."""
    description = STATE_LABEL_SPECS[STATE_IMPLEMENTATION_GO]["description"]

    assert "reviewed-head proof" in description
    assert "drive-green may proceed" not in description
