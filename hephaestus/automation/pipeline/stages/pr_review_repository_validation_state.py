"""Evaluate bound repository-validation state without host I/O."""

import json

from ..repository_validation import (
    RepositoryValidationAttempt,
    RepositoryValidationCoverage,
    RepositoryValidationGap,
    validation_attempt_coverage,
)
from ..work_item import WorkItem


def _repository_validation_coverage(item: WorkItem) -> RepositoryValidationCoverage:
    """Bind live attempt coverage to the current review identity."""
    attempt = item.payload.get("repository_validation_attempt")
    generation = item.payload.get("reviewed_pr_proof_generation")
    if (
        type(attempt) is not RepositoryValidationAttempt
        or type(generation) is not int
        or attempt.generation != generation
        or attempt.plan.repository.casefold() != f"llm360/{item.repo.casefold()}"
        or attempt.plan.pr_number != item.pr
        or attempt.plan.issue_number != item.issue
        or attempt.plan.reviewed_head != item.payload.get("pr_head_sha")
        or attempt.plan.reviewed_base != item.payload.get("reviewed_pr_base_sha")
        or item.payload.get("repository_validation_failure")
    ):
        return RepositoryValidationCoverage(
            "gap", gaps=(RepositoryValidationGap("*", "validation_stage_identity_invalid"),)
        )
    return validation_attempt_coverage(
        attempt,
        reviewed_head=str(item.payload.get("reviewed_pr_head_sha") or ""),
        reviewed_base=str(item.payload.get("pr_base_sha") or ""),
    )


def _repository_validation_required(item: WorkItem, org: str | None) -> bool:
    """Select the repository that requires a bound validation attempt."""
    return item.repo.casefold() == "comet" and (org is None or org.casefold() == "llm360")


def _repository_validation_complete(item: WorkItem, org: str | None) -> bool:
    """Require complete current coverage for the selected repository."""
    return not _repository_validation_required(item, org) or (
        _repository_validation_coverage(item).status == "complete"
    )


def _repository_validation_prompt_json(item: WorkItem, org: str | None) -> str:
    """Describe current admitted evidence without a second receipt inventory."""
    if not _repository_validation_required(item, org):
        return ""
    coverage = _repository_validation_coverage(item)
    summary: dict[str, object] = {
        "schema": "repository-validation-summary-v1",
        "status": coverage.status,
        "gaps": [{"check_id": gap.check_id, "reason": gap.reason} for gap in coverage.gaps],
        "uncovered_check_ids": list(coverage.uncovered_check_ids),
    }
    if coverage.status == "complete":
        attempt = item.payload["repository_validation_attempt"]
        plan = attempt.plan
        summary.update(
            {
                "repository": plan.repository,
                "pr_number": plan.pr_number,
                "reviewed_head": plan.reviewed_head,
                "reviewed_base": plan.reviewed_base,
                "plan_id": plan.plan_id,
                "profile_id": plan.profile_id,
                "profile_digest": plan.profile_digest,
                "generation": attempt.generation,
                "checks": [
                    {"check_id": check.check_id, "argv": list(check.argv)} for check in plan.checks
                ],
                "receipts": [
                    {
                        "check_id": receipt.check_id,
                        "receipt_id": receipt.receipt_id,
                        "evidence_kind": receipt.evidence_kind,
                        "status": receipt.status,
                    }
                    for receipt in coverage.receipts
                ],
            }
        )
    return json.dumps(summary, sort_keys=True, separators=(",", ":"))
