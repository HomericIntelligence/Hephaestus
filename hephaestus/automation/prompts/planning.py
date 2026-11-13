"""Planning-phase prompts.

Contains the plan-generation prompt, the standalone plan-review prompt, and
the iteration-aware plan-loop review prompt.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from hephaestus.prompts import PromptCatalog

from ._review_rubric import (
    get_full_sweep_suffix,
    get_plan_loop_review_rubric,
    get_plan_review_output_format,
    get_plan_review_rubric,
)
from ._shared import (
    FencedContent,
    _iteration_guidance,
    _iteration_label,
    _prior_review_block,
    fence_content,
    get_terse_output_directive,
)

_PLAN_REVIEW_TERMINAL_OUTPUT_CONTRACT = (
    "For plan review, do not add a separate conclusion line. Follow the "
    "plan-state contract below; its final state label is the only decision."
)


def get_plan_prompt(issue_number: int, *, catalog: PromptCatalog | None = None) -> str:
    """Get the planning prompt for an issue."""
    return (catalog or PromptCatalog.current()).render(
        "planning/plan.j2",
        issue_number=issue_number,
        terse_output_directive=get_terse_output_directive(),
    )


def get_plan_review_prompt(
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
) -> str:
    """Get the plan review prompt for evaluating an issue implementation plan.

    Args:
        issue_number: GitHub issue number
        issue_title: Issue title (fenced as untrusted)
        issue_body: Issue body/description (fenced as untrusted)
        plan_text: The full plan text to review (fenced as untrusted)

    Returns:
        Formatted plan review prompt

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/plan_review.j2",
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        untrusted_notice=fenced.untrusted_notice,
        review_rubric=get_plan_review_rubric().strip(),
        output_format=get_plan_review_output_format().strip(),
        terse_output_directive=get_terse_output_directive(
            terminal_output_contract=_PLAN_REVIEW_TERMINAL_OUTPUT_CONTRACT
        ),
    )


def get_plan_loop_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
    learnings: str,
    iteration: int,
    prior_review: str | None,
    advise_findings: str = "",
    plan_history: str = "",
    planning_cycle_id: str = "",
    reviewer_session_id: str = "",
    plan_revision: int = 1,
    plan_fingerprint: str = "",
    review_transcript: str = "",
) -> str:
    """Build the iteration-aware plan-loop review prompt.

    Args:
        issue_number: GitHub issue number.
        issue_title: Issue title.
        issue_body: Full issue body.
        plan_text: Plan to review.
        learnings: Learnings captured by the planner this iteration.
        iteration: Iteration index (0, 1, or 2).
        prior_review: Previous iteration's review text, or ``None`` on iter 0.
        advise_findings: Prior team learnings from the advise step to give the
            reviewer the same Mnemosyne context the planner received.

    Returns:
        Formatted prompt for a fresh review iteration.

    """
    fenced = fence_content()
    full_sweep_suffix = get_full_sweep_suffix().strip() if iteration == 2 else ""
    return PromptCatalog.current().render(
        "planning/plan_loop_review.j2",
        rubric=get_plan_loop_review_rubric().strip(),
        iteration=iteration,
        iteration_label=_iteration_label(iteration),
        iteration_guidance=_iteration_guidance(iteration),
        issue_number=issue_number,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        advise_findings_block=fenced.fence(
            "ADVISE_FINDINGS",
            advise_findings or "_(no prior advise findings supplied)_",
        ),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        learnings=learnings or "_(no learnings captured this iteration)_",
        prior_review_block=_prior_review_block(prior_review, fenced),
        plan_history_block=(
            fenced.fence("PLAN_HISTORY", plan_history) if plan_history else "_(first revision)_"
        ),
        planning_cycle_id=planning_cycle_id,
        reviewer_session_id=reviewer_session_id,
        plan_revision=plan_revision,
        plan_fingerprint=plan_fingerprint,
        review_transcript_block=(
            fenced.fence("REVIEW_TRANSCRIPT", review_transcript)
            if review_transcript
            else "_(no prior review transcript)_"
        ),
        full_sweep_suffix=full_sweep_suffix,
        output_format=get_plan_review_output_format().strip(),
        untrusted_notice=fenced.untrusted_notice,
        terse_output_directive=get_terse_output_directive(
            terminal_output_contract=_PLAN_REVIEW_TERMINAL_OUTPUT_CONTRACT
        ),
    )


def _manifest_block(
    fenced: FencedContent,
    manifest: Sequence[Mapping[str, str]],
) -> str:
    """Render one host-owned identifier manifest as canonical JSON."""
    payload = json.dumps([dict(entry) for entry in manifest], sort_keys=True, separators=(",", ":"))
    return fenced.fence("IDENTIFIER_MANIFEST", payload)


def get_plan_decomposition_prompt(
    *,
    issue_number: int,
    issue_title: str,
    issue_body: str,
    plan_text: str,
    requirement_manifest: Sequence[Mapping[str, str]],
    finding_manifest: Sequence[Mapping[str, str]],
    requirements_epoch: str,
    source_plan_revision: int,
    source_plan_fingerprint: str,
    rejected_draft_digest: str = "",
    validation_errors: Sequence[str] = (),
    review_findings: str = "",
) -> str:
    """Build the one-shot decomposition generation prompt.

    All GitHub-derived inputs are fenced as untrusted data. The host-owned
    manifests, epoch, revision, and fingerprint are context, not instructions.

    Args:
        issue_number: Parent issue number.
        issue_title: Parent issue title.
        issue_body: Parent issue body.
        plan_text: Complete normalized current plan.
        requirement_manifest: Host-owned requirement identifier manifest.
        finding_manifest: Host-owned finding identifier manifest.
        requirements_epoch: Requirements epoch digest.
        source_plan_revision: Plan revision the tree decomposes.
        source_plan_fingerprint: Fingerprint of that plan text.
        rejected_draft_digest: Digest of a rejected replacement target.
        validation_errors: Host validation errors for the rejected draft.
        review_findings: Independent review findings to address.

    Returns:
        The complete generation prompt with the writing-standard directive.

    """
    fenced = fence_content()
    return PromptCatalog.current().render(
        "planning/plan_decomposition.j2",
        issue_number=issue_number,
        terse_output_directive=get_terse_output_directive(),
        untrusted_notice=fenced.untrusted_notice,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        issue_body_block=fenced.fence("ISSUE_BODY", issue_body),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        requirement_manifest_block=_manifest_block(fenced, requirement_manifest),
        finding_manifest_block=_manifest_block(fenced, finding_manifest),
        requirements_epoch=requirements_epoch,
        source_plan_revision=source_plan_revision,
        source_plan_fingerprint=source_plan_fingerprint,
        rejected_draft_digest=rejected_draft_digest,
        validation_errors=list(validation_errors),
        review_findings=review_findings,
        review_findings_block=(
            fenced.fence("REVIEW_FINDINGS", review_findings) if review_findings else ""
        ),
    )


def get_plan_decomposition_review_prompt(
    *,
    issue_number: int,
    issue_title: str,
    plan_text: str,
    draft_digest: str,
    tree_json: str,
    requirement_manifest: Sequence[Mapping[str, str]],
    finding_manifest: Sequence[Mapping[str, str]],
    layers: Sequence[Sequence[str]],
) -> str:
    """Build the one-shot independent decomposition review prompt.

    The receipt binds to the exact draft digest. Model output stays untrusted
    data; only the host validates and publishes it.

    Args:
        issue_number: Parent issue number.
        issue_title: Parent issue title.
        plan_text: Complete normalized current plan.
        draft_digest: Exact normalized draft digest under review.
        tree_json: Canonical JSON of the complete draft tree.
        requirement_manifest: Host-owned requirement identifier manifest.
        finding_manifest: Host-owned finding identifier manifest.
        layers: Topological node layers of the draft tree.

    Returns:
        The complete review prompt with the writing-standard directive.

    """
    fenced = fence_content()
    layers_json = json.dumps([list(layer) for layer in layers])
    return PromptCatalog.current().render(
        "planning/plan_decomposition_review.j2",
        issue_number=issue_number,
        terse_output_directive=get_terse_output_directive(),
        untrusted_notice=fenced.untrusted_notice,
        issue_title_block=fenced.fence("ISSUE_TITLE", issue_title),
        plan_text_block=fenced.fence("PLAN_TEXT", plan_text),
        draft_digest_block=fenced.fence("DRAFT_DIGEST", draft_digest),
        tree_block=fenced.fence("DRAFT_TREE", tree_json),
        requirement_manifest_block=_manifest_block(fenced, requirement_manifest),
        finding_manifest_block=_manifest_block(fenced, finding_manifest),
        layers_block=fenced.fence("TOPOLOGICAL_LAYERS", layers_json),
        draft_digest=draft_digest,
    )
