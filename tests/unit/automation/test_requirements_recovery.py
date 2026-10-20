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
    HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
    RecoveryCommentIdentityError,
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
    recovered_requirements_json,
    render_recovered_requirements,
    select_recovery_comment,
    verified_finalized_plan,
)
from hephaestus.automation.review_journal import HISTORY_MARKER


def _finalized_body(
    content: str = "## Why\n\nPreserve the approved behavior.",
    *,
    prefix: str = HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
) -> str:
    """Return a body carrying a correctly self-bound finalized-plan marker."""
    placeholder = (
        f"{content}\n\n{prefix}R={'a' * 64} P=123456789:{'b' * 64} V=987654321:{'c' * 64} F=<F> -->"
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


def test_verified_shared_finalized_body_is_not_recovered_as_generated_requirements() -> None:
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


def test_shared_homeric_intelligence_finalized_body_is_recognized() -> None:
    """Finalized output from the shared planning contract remains sealed evidence."""
    assert HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX == "<!-- HomericIntelligence:finalize-plan "
    body = _finalized_body()

    assert verified_finalized_plan(body) is not None
    assert has_contaminated_issue_body(body) is False


def test_legacy_athena_finalized_body_remains_recognized() -> None:
    """Existing sealed Athena bodies remain valid through the one-write migration."""
    body = _finalized_body(prefix=ATHENA_FINALIZED_PLAN_PREFIX)

    assert verified_finalized_plan(body) is not None
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
        HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
        indent + HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
    )

    assert verified_finalized_plan(body) is None
    assert has_contaminated_issue_body(body) is True


def test_four_space_indented_finalized_example_is_not_top_level_authority() -> None:
    body = _finalized_body().replace(
        HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
        "    " + HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
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


def test_recovery_parser_rejects_empty_model_output_with_json_decode_cause() -> None:
    """An empty planner response must fail closed and keep the decode cause.

    The automation loop log recorded ``JSONDecodeError: Expecting value: line 1
    column 1 (char 0)`` followed by ``recovery output must be one JSON object``
    for issue 2315. The position proves that the value supplied to the parser
    began with no JSON token at all.

    The parser is not the layer at fault. Issue 3399 shows the agent supplied a
    contract-valid answer and ``_parse_opencode_json_events`` discarded it when
    the stream carried a trailing empty text event. These cases therefore
    characterize what the contract does with whatever text reaches it. They do
    not claim the model returned nothing.

    The decode cause is asserted only when the parser supplies one. A future
    hardening of ``_json_object`` may raise without chaining a decode error, and
    that must not block the fix.
    """
    with pytest.raises(ValueError, match="one JSON object") as excinfo:
        parse_recovered_requirements("")

    cause = excinfo.value.__cause__
    if cause is not None:
        assert isinstance(cause, json.JSONDecodeError)
        assert cause.pos == 0
        assert cause.lineno == 1
        assert cause.colno == 1


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("   \n\t  \r\n", id="whitespace-only"),
        pytest.param("I could not determine the requirements for this issue.", id="prose"),
        pytest.param("ERROR: upstream provider returned an error", id="provider-error-text"),
        pytest.param('{"disposition":"REQUIREMENTS"', id="truncated-object"),
        pytest.param("null", id="json-null"),
        pytest.param('"a bare string"', id="json-string"),
        pytest.param("42", id="json-number"),
    ],
)
def test_recovery_parser_fails_closed_on_non_object_output(raw: str) -> None:
    """Every non-object planner response must raise the one-object error.

    These shapes carry no JSON object anywhere, so the outcome is stable. They
    do not depend on how the parser searches for one.

    Six shapes were removed from this list. Issue 3399 candidate fix 1 makes
    them parse, so asserting that they fail would block the fix:

    - an upper-case ```` ```JSON ```` fence
    - a fenced block with trailing prose
    - a prose preamble followed by an object
    - a balanced object followed by trailing data
    - a JSON array that contains an object
    - two balanced objects in a row, in the review parser

    ``trailing-data`` and ``array-not-object`` were confirmed against three
    plausible readings of the fix. The first decodes the object at offset 0
    under every reading that scans for a balanced object. The second fails
    under a naive scan of every opening brace, which is the most natural way to
    write that scan.

    Add each removed shape as an accepted case in the same commit that lands
    the fix. The issue lists the prose-preamble shape in its own coverage table,
    so the fix must accept it.
    """
    with pytest.raises(ValueError, match="one JSON object"):
        parse_recovered_requirements(raw)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": None, "reason": "r", "evidence": "e"},
            "requirements must be a string",
            id="requirements-null",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": ["r"], "reason": "r", "evidence": "e"},
            "requirements must be a string",
            id="requirements-list",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": 7, "reason": "r", "evidence": "e"},
            "requirements must be a string",
            id="requirements-number",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": "r", "reason": None, "evidence": "e"},
            "reason must be a string",
            id="reason-null",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": "r", "reason": "   ", "evidence": "e"},
            "reason must not be empty",
            id="reason-whitespace-only",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": "r", "reason": "r", "evidence": ""},
            "evidence must not be empty",
            id="evidence-empty",
        ),
        pytest.param(
            {"disposition": "MAYBE", "requirements": "", "reason": "r", "evidence": "e"},
            "invalid recovery disposition",
            id="unknown-disposition",
        ),
        pytest.param(
            {"disposition": None, "requirements": "", "reason": "r", "evidence": "e"},
            "invalid recovery disposition",
            id="null-disposition",
        ),
        pytest.param(
            {"disposition": "REQUIREMENTS", "requirements": "r", "reason": "r"},
            "fields do not match",
            id="missing-evidence",
        ),
        pytest.param(
            {
                "disposition": "TRACKER",
                "requirements": "not allowed",
                "reason": "r",
                "evidence": "e",
            },
            "must not contain requirements",
            id="tracker-with-requirements",
        ),
        pytest.param(
            {
                "disposition": "OBSOLETE",
                "requirements": "not allowed",
                "reason": "r",
                "evidence": "e",
            },
            "must not contain requirements",
            id="obsolete-with-requirements",
        ),
    ],
)
def test_recovery_parser_fails_closed_on_field_contract_drift(
    payload: dict[str, object], message: str
) -> None:
    """Every field-level contract violation must raise its own message."""
    with pytest.raises(ValueError, match=message):
        parse_recovered_requirements(json.dumps(payload))


def test_recovery_parser_accepts_exactly_fenced_json() -> None:
    """The lowercase JSON fence must be stripped before parsing.

    A model that wraps its object in one lowercase ```` ```json ```` fence is
    still conforming. The strip must stay exact so that the malformed shapes in
    the other tests keep failing.
    """
    fenced = (
        "```json\n"
        '{"disposition":"REQUIREMENTS","requirements":"Keep the flag default.",'
        '"reason":"The flag is documented as stable.","evidence":"The public API binds it."}\n'
        "```"
    )

    result = parse_recovered_requirements(fenced)

    assert result.disposition is RecoveryDisposition.REQUIREMENTS
    assert result.requirements == "Keep the flag default."


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("", id="empty"),
        pytest.param("No verdict.", id="prose"),
        pytest.param(
            '{"verdict":"GO","disposition":"MAYBE","reason":"x"}', id="unknown-disposition"
        ),
        pytest.param('{"verdict":"GO","disposition":"OBSOLETE","reason":""}', id="empty-reason"),
    ],
)
def test_recovery_review_parser_fails_closed_on_malformed_output(raw: str) -> None:
    """The independent reviewer must fail closed on the same malformed shapes.

    The trailing-data case was removed. Issue 3399 candidate fix 1 reads that
    shape as one balanced object followed by a second candidate, and at least
    one reading of the issue accepts it. Add it back as an accepted case if the
    landed fix accepts it.
    """
    with pytest.raises(ValueError):
        parse_recovery_review(raw)


def test_recovered_requirements_json_round_trips_through_the_parser() -> None:
    """The deterministic reviewer payload must re-parse as a valid proposal.

    The independent reviewer receives ``recovered_requirements_json`` output.
    A drift between the serializer and the parser would fail every reviewed
    proposal, so the two must stay bound.
    """
    proposal = parse_recovered_requirements(
        json.dumps(
            {
                "disposition": "OBSOLETE",
                "requirements": "",
                "reason": "A merged replacement supersedes this request.",
                "evidence": "The replacement commit closes the reported gap.",
            }
        )
    )

    reparsed = parse_recovered_requirements(recovered_requirements_json(proposal))

    assert reparsed == proposal


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


def _recovery_body(version: int = 3) -> str:
    """Return a correct recovery body for selector tests."""
    body = render_recovered_requirements(
        "source requirements",
        "recovered requirements",
        "b" * 64,
        issue_title="Issue title",
        repository_revision="c" * 40,
    )
    if version < 3:
        body = render_recovered_requirements(
            "source requirements",
            "recovered requirements",
            "b" * 64,
        )
        if version == 1:
            body = body.replace(":v=2:", ":v=1:", 1)
    return body


def _recovery_comment(body: str, *, owned: bool = True, comment_id: int = 1) -> dict[str, object]:
    """Return metadata shaped like a bounded issue-comment journal entry."""
    return {"body": body, "databaseId": comment_id, "owned": owned}


@pytest.mark.parametrize("version", [1, 2, 3])
def test_recovery_selector_accepts_each_supported_version(version: int) -> None:
    body = _recovery_body(version)
    comment = _recovery_comment(body)

    selected = select_recovery_comment(
        [comment],
        body_of=lambda value: str(value["body"]),
        owned_of=lambda value: bool(value["owned"]),
    )

    assert selected is not None
    assert selected.comment is comment
    assert selected.provenance.version == version


def test_recovery_selector_rejects_leading_whitespace_before_marker() -> None:
    body = "  " + _recovery_body()

    with pytest.raises(RecoveryCommentIdentityError, match="byte zero"):
        select_recovery_comment(
            [_recovery_comment(body)],
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )


def test_recovery_selector_rejects_foreign_marker() -> None:
    with pytest.raises(RecoveryCommentIdentityError, match="foreign"):
        select_recovery_comment(
            [_recovery_comment(_recovery_body(), owned=False)],
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )


def test_recovery_selector_rejects_malformed_marker() -> None:
    malformed = _recovery_body().replace(":v=3:", ":v=9:", 1)

    with pytest.raises(RecoveryCommentIdentityError, match="malformed"):
        select_recovery_comment(
            [_recovery_comment(malformed)],
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )


def test_recovery_selector_rejects_repeated_claims_in_one_comment() -> None:
    first = _recovery_body()
    second = _recovery_body().split("\n", 1)[0]
    repeated = f"{first}\n\n{second}\n\nrepeated"

    with pytest.raises(RecoveryCommentIdentityError, match="repeated"):
        select_recovery_comment(
            [_recovery_comment(repeated)],
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )


def test_recovery_selector_rejects_duplicate_actor_owned_comments() -> None:
    comments = [
        _recovery_comment(_recovery_body(), comment_id=1),
        _recovery_comment(_recovery_body(), comment_id=2),
    ]

    with pytest.raises(RecoveryCommentIdentityError, match="duplicate"):
        select_recovery_comment(
            comments,
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )


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

    assert "Broken retry" in recovery
    assert "Do not include diffs" in recovery
    assert "minimal" in recovery.lower()
    assert "independent" in review.lower()
    assert "acme/repo" in review
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
