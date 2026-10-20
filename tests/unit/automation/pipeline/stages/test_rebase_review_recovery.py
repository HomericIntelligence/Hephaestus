"""Shared retained rebase review record builders for stage tests."""

from hephaestus.automation.pipeline.rebase_review import RebaseReviewProof
from hephaestus.automation.rebase_review_receipt import (
    RebaseReviewRecord,
    original_audit_identity,
)
from hephaestus.automation.review_audit import ReviewAudit


def _record() -> RebaseReviewRecord:
    return RebaseReviewRecord(
        repository="test-org/test-repo",
        issue_number=1,
        pr_number=12,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=original_audit_identity(12, "a" * 40),
        audit=ReviewAudit(
            grade="A",
            summary="No findings.",
            findings=(),
            raw_feedback="",
            valid=True,
            verdict="GO",
        ),
    )


def _proof() -> RebaseReviewProof:
    return RebaseReviewProof(
        repository="test-org/test-repo",
        issue_number=1,
        pr_number=12,
        reviewed_head_sha="a" * 40,
        reviewed_base_sha="b" * 40,
        source_head_sha="a" * 40,
        target_base_sha="c" * 40,
        resulting_head_sha="d" * 40,
        resulting_tree_sha="e" * 40,
        original_audit_id=original_audit_identity(12, "a" * 40),
    )


__all__ = ["_proof", "_record"]
