"""Pure requirements-recovery protocol tests."""
# ruff: noqa: D103

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

import pytest

from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.requirements_recovery import (
    ATHENA_FINALIZED_PLAN_PREFIX,
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
    recovered_requirements_for_context,
    render_recovered_requirements,
    verified_finalized_plan,
)
from hephaestus.automation.review_journal import HISTORY_MARKER


def _finalized_body(content: str = "## Why\n\nPreserve the approved behavior.") -> str:
    """Return a body carrying a correctly self-bound Athena final marker."""
    placeholder = (
        f"{content}\n\n{ATHENA_FINALIZED_PLAN_PREFIX}"
        f"R={'a' * 64} P=123456789:{'b' * 64} "
        f"V=987654321:{'c' * 64} F=<F> -->"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    return placeholder.replace("F=<F>", f"F={digest}")


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


def test_verified_athena_finalized_body_is_not_recovered_as_generated_requirements() -> None:
    body = _finalized_body()

    identity = verified_finalized_plan(body)

    assert identity is not None
    assert identity.requirements_identity == "a" * 64
    assert identity.plan_identity == f"123456789:{'b' * 64}"
    assert identity.review_identity == f"987654321:{'c' * 64}"
    assert (
        identity.final_body_digest
        == hashlib.sha256(
            body.replace(f"F={identity.final_body_digest}", "F=<F>").encode("utf-8")
        ).hexdigest()
    )
    assert has_contaminated_issue_body(body) is False


def test_self_checksummed_role_names_are_not_comment_identities() -> None:
    """Artifact roles cannot stand in for exact GitHub issue-comment IDs."""
    placeholder = (
        "## Why\n\nPreserve the approved behavior.\n\n"
        f"{ATHENA_FINALIZED_PLAN_PREFIX}R={'a' * 64} "
        f"P=plan-comment:{'b' * 64} V=review-comment:{'c' * 64} F=<F> -->"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    body = placeholder.replace("F=<F>", f"F={digest}")

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


@pytest.mark.parametrize("indent", [" ", "  ", "   "])
def test_indented_finalized_claim_fails_closed(indent: str) -> None:
    """Top-level Markdown indentation cannot hide an invalid finalization claim."""
    body = _finalized_body().replace(
        ATHENA_FINALIZED_PLAN_PREFIX,
        indent + ATHENA_FINALIZED_PLAN_PREFIX,
    )

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


def test_four_space_indented_finalized_example_is_not_top_level_authority() -> None:
    body = _finalized_body().replace(
        ATHENA_FINALIZED_PLAN_PREFIX,
        "    " + ATHENA_FINALIZED_PLAN_PREFIX,
    )

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is False


def test_invalid_backtick_fence_info_cannot_hide_finalized_claim() -> None:
    marker = _finalized_body().splitlines()[-1]
    body = f"```bad`info\n  {marker}\n```"

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


def test_list_child_fence_cannot_hide_dedented_finalized_claim() -> None:
    marker = _finalized_body().splitlines()[-1]
    body = f"- example\n\n  ```text\n  fenced text\n{marker}"

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


def test_finalized_marker_inside_list_is_not_top_level_authority() -> None:
    marker = _finalized_body().splitlines()[-1]
    body = f"- example\n\n  {marker}"

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is False


@pytest.mark.parametrize(
    "unicode_separator",
    ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"],
)
def test_unicode_separator_cannot_desynchronize_commonmark_line_mapping(
    unicode_separator: str,
) -> None:
    marker = _finalized_body().splitlines()[-1]
    body = f"prefix{unicode_separator}same CommonMark line\n{marker}"

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [("<!--\n", "\n-->"), ("<script>\n", "\n</script>"), ("<div>\n", "\n</div>")],
)
def test_nested_raw_html_marker_is_not_standalone_authority(prefix: str, suffix: str) -> None:
    placeholder = (
        f"{prefix}{ATHENA_FINALIZED_PLAN_PREFIX}R={'a' * 64} "
        f"P=123456789:{'b' * 64} V=987654321:{'c' * 64} F=<F> -->{suffix}"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    body = placeholder.replace("F=<F>", f"F={digest}")

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is False


def test_plan_and_review_must_reference_distinct_comments() -> None:
    placeholder = (
        "## Why\n\nPreserve independently reviewed authority.\n\n"
        f"{ATHENA_FINALIZED_PLAN_PREFIX}R={'a' * 64} "
        f"P=123456789:{'b' * 64} V=123456789:{'c' * 64} F=<F> -->"
    )
    digest = hashlib.sha256(placeholder.encode("utf-8")).hexdigest()
    body = placeholder.replace("F=<F>", f"F={digest}")

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


def test_recovered_requirements_bind_title_body_issue_repo_and_revision() -> None:
    source = "Original requirements"
    revision = "d" * 40
    binding = evidence_digest("repo", 17, revision, "Original title", source)
    rendered = render_recovered_requirements(
        source,
        "Recovered requirements",
        binding,
        issue_title="Original title",
        repository_revision=revision,
    )

    assert (
        recovered_requirements_for_context(
            rendered,
            repository="repo",
            issue_number=17,
            issue_title="Original title",
            source_body=source,
            repository_revision=revision,
        )
        == "Recovered requirements"
    )
    assert (
        recovered_requirements_for_context(
            rendered,
            repository="repo",
            issue_number=17,
            issue_title="Edited title",
            source_body=source,
            repository_revision=revision,
        )
        is None
    )
    assert (
        recovered_requirements_for_context(
            rendered,
            repository="repo",
            issue_number=17,
            issue_title="Original title",
            source_body=source,
            repository_revision="e" * 40,
        )
        is None
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body + "\nLater material edit.",
        lambda body: body.replace("R=" + "a" * 64, "R=malformed identity"),
        lambda body: body.replace("P=123456789:" + "b" * 64, "P=<P>"),
        lambda body: body.replace("P=123456789:" + "b" * 64, "P=" + "b" * 64),
        lambda body: body.replace("P=123456789:", "P=plan-comment:"),
        lambda body: body.replace("P=123456789:" + "b" * 64, "P=123456789:x"),
        lambda body: body + "\n" + body.splitlines()[-1],
        lambda body: body.replace("F=", "F=0", 1),
    ],
)
def test_invalid_or_drifted_athena_finalization_requires_recovery(
    mutate: Callable[[str], str],
) -> None:
    body = mutate(_finalized_body())

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


@pytest.mark.parametrize(
    "body",
    [
        "Document <!-- athena:finalize-plan R=... P=... V=... F=... --> in the guide.",
        "```text\n<!-- athena:finalize-plan R=x P=y V=z F=0 -->\n```",
    ],
)
def test_nonsemantic_finalized_marker_example_is_ordinary_requirement_text(body: str) -> None:

    assert verified_finalized_plan(body) is None
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
        repository="acme/repo",
        repository_revision="a" * 40,
    )

    assert "b" * 64 in recovery and "a" * 40 in recovery
    assert f"The `evidence` value MUST be exactly `{'b' * 64}`" in recovery
    assert "Do not include diffs" in recovery
    assert "minimal" in recovery.lower()
    assert "independent" in review.lower()
    assert "acme/repo" in review and "a" * 40 in review
    assert "bound checkout" in review
    assert "GO or NOGO" in review
    assert "BLOCKED" not in review


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("Epic: automation reliability", "Child issue checklist"),
        ("Roadmap Q4", "Milestones"),
        ("Q3 Roadmap tracking", "Milestones"),
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
