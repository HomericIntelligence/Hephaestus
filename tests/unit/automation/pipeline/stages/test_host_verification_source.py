"""Tests that both pipeline stages use the canonical host-verification source."""

from __future__ import annotations

from hephaestus.git.verification import (
    EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON,
    EMPTY_HOST_VERIFICATIONS_JSON,
    HEPHAESTUS_REQUIRED_CHECK_ARGV,
    HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S,
    HOST_VERIFICATION_WORKSPACE_KEY,
    PRE_PR_TEST_ARGV,
    publish_host_verification_failure,
)
from hephaestus.git.verification.specs import (
    HEPHAESTUS_REQUIRED_CHECK,
    PRE_PR_TEST,
)


def test_implementation_stage_reexports_canonical_specs() -> None:
    """The implementation stage owns no second argv definition."""
    from hephaestus.automation.pipeline.stages.implementation import implementation as stage

    assert stage.PRE_PR_TEST_ARGV is PRE_PR_TEST_ARGV
    assert stage.HEPHAESTUS_REQUIRED_CHECK_ARGV is HEPHAESTUS_REQUIRED_CHECK_ARGV
    assert stage.HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S == HEPHAESTUS_REQUIRED_CHECK_TIMEOUT_S
    assert stage.publish_host_verification_failure is publish_host_verification_failure


def test_pr_review_stage_reexports_canonical_defaults() -> None:
    """The review stage uses the shared workspace key and empty defaults."""
    from hephaestus.automation.pipeline.stages.pr_review import pr_review_jobs, pr_review_post

    assert HOST_VERIFICATION_WORKSPACE_KEY == "host_verification_workspace"
    assert pr_review_jobs.EMPTY_HOST_VERIFICATIONS_JSON == EMPTY_HOST_VERIFICATIONS_JSON
    assert (
        pr_review_jobs.EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON
        == EMPTY_HOST_VERIFICATION_BOOTSTRAP_JSON
    )
    assert pr_review_post.EMPTY_HOST_VERIFICATIONS_JSON == EMPTY_HOST_VERIFICATIONS_JSON
    assert PRE_PR_TEST.argv == PRE_PR_TEST_ARGV
    assert HEPHAESTUS_REQUIRED_CHECK.argv == HEPHAESTUS_REQUIRED_CHECK_ARGV
