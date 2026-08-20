"""Pure requirements-recovery protocol tests."""
# ruff: noqa: D103

from __future__ import annotations

import hashlib
import json

import pytest

from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.requirements_recovery import (
    RecoveryDisposition,
    RecoveryVerdict,
    build_recovery_prompt,
    build_recovery_review_prompt,
    evidence_digest,
    has_contaminated_issue_body,
    is_semantic_disposition_candidate,
    parse_recovered_requirements,
    parse_recovery_provenance,
    parse_recovery_review,
    render_recovered_requirements,
)
from hephaestus.automation.review_journal import HISTORY_MARKER


@pytest.mark.parametrize(
    "first_line",
    [
        PLAN_CANONICAL_MARKER,
        PLAN_REVIEW_CANONICAL_MARKER,
        HISTORY_MARKER.format(revision=4, kind="plan"),
        HISTORY_MARKER.format(revision=4, kind="review"),
    ],
)
def test_exact_leading_automation_marker_requires_recovery(first_line: str) -> None:
    assert has_contaminated_issue_body(f"\n  {first_line}\nDerived text") is True


@pytest.mark.parametrize(
    "body",
    [
        "Requirements mention <!-- hephaestus-plan:canonical --> later.",
        "# Implementation Plan\nHuman-authored heading",
        "<!-- hephaestus-plan:canonical-extra -->\nNot exact",
        "",
    ],
)
def test_noncanonical_or_nonleading_markers_are_not_contamination(body: str) -> None:
    assert has_contaminated_issue_body(body) is False


def test_recovery_parser_is_strict_and_typed() -> None:
    raw = json.dumps(
        {
            "disposition": "REQUIREMENTS",
            "requirements": "Preserve the API and add a regression test.",
            "reason": "The original request is recoverable from repository evidence.",
            "evidence": "The public API and failing test identify the intended behavior.",
        }
    )

    result = parse_recovered_requirements(raw)

    assert result.disposition is RecoveryDisposition.REQUIREMENTS
    assert "Preserve the API" in result.requirements


def test_recovery_parser_rejects_unknown_fields_and_missing_requirements() -> None:
    with pytest.raises(ValueError, match="fields"):
        parse_recovered_requirements(
            '{"disposition":"TRACKER","requirements":"","reason":"r",'
            '"evidence":"e","approval":true}'
        )
    with pytest.raises(ValueError, match="requirements"):
        parse_recovered_requirements(
            '{"disposition":"REQUIREMENTS","requirements":"","reason":"r","evidence":"e"}'
        )


def test_independent_review_parser_has_only_binary_verdicts() -> None:
    result = parse_recovery_review(
        '{"verdict":"GO","disposition":"OBSOLETE",'
        '"reason":"A merged replacement makes this request unnecessary."}'
    )

    assert result.verdict is RecoveryVerdict.GO
    assert result.disposition is RecoveryDisposition.OBSOLETE
    with pytest.raises(ValueError, match="verdict"):
        parse_recovery_review('{"verdict":"APPROVED","disposition":"OBSOLETE","reason":"x"}')
    with pytest.raises(ValueError, match="verdict"):
        parse_recovery_review('{"verdict":"BLOCKED","disposition":"OBSOLETE","reason":"x"}')


def test_provenance_round_trip_binds_all_three_digests() -> None:
    source = f"{PLAN_CANONICAL_MARKER}\nOld plan"
    requirements = "## Required behavior\n\n- Keep the API stable."
    binding = evidence_digest("repo", 17, "a" * 40, "title", source)

    body = render_recovered_requirements(source, requirements, binding)
    provenance = parse_recovery_provenance(body)

    assert provenance is not None
    assert provenance.source_digest == hashlib.sha256(source.encode()).hexdigest()
    assert provenance.requirements_digest == hashlib.sha256(requirements.encode()).hexdigest()
    assert provenance.evidence_digest == binding
    assert has_contaminated_issue_body(body) is False


def test_tampered_recovered_body_does_not_validate_provenance() -> None:
    body = render_recovered_requirements("source", "requirements", "b" * 64)
    assert parse_recovery_provenance(body.replace("requirements", "changed")) is None


def test_prompts_bind_evidence_and_minimize_repeated_code() -> None:
    recovery = build_recovery_prompt(
        issue_number=17,
        issue_title="Broken retry",
        issue_body="Derived plan",
        repository="acme/repo",
        repository_revision="a" * 40,
        evidence_binding="b" * 64,
    )
    review = build_recovery_review_prompt(
        issue_number=17,
        issue_title="Broken retry",
        issue_body="Derived plan",
        source_body_digest="c" * 64,
        evidence_binding="b" * 64,
        proposal_json=(
            '{"disposition":"REQUIREMENTS","requirements":"Keep retry bounded.",'
            '"reason":"Repository evidence","evidence":"Tests"}'
        ),
    )

    assert "b" * 64 in recovery and "a" * 40 in recovery
    assert "Do not include diffs" in recovery
    assert "minimal" in recovery.lower()
    assert "independent" in review.lower()
    assert "GO or NOGO" in review
    assert "BLOCKED" not in review


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Epic: automation reliability", "Child issue checklist"),
        ("Roadmap Q4", "Milestones"),
        ("A normal bug", "This request is already resolved by merged PR #9."),
        ("Obsolete: old migration", "Superseded by the new migration."),
    ],
)
def test_semantic_disposition_candidates_are_narrow(title: str, body: str) -> None:
    assert is_semantic_disposition_candidate(title, body) is True


def test_ordinary_tasks_are_not_semantic_skip_candidates() -> None:
    assert (
        is_semantic_disposition_candidate(
            "Delete obsolete module", "Remove the deprecated implementation and add tests."
        )
        is False
    )
