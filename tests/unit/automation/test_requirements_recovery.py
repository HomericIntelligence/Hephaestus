"""Pure requirements-recovery protocol tests."""
# ruff: noqa: D103

from __future__ import annotations

import json

import pytest

from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
)
from hephaestus.automation.requirements_recovery import (
    ATHENA_FINALIZED_PLAN_PREFIX,
    HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
    RECOVERED_REQUIREMENTS_MARKER,
    RecoveryDisposition,
    RecoveryVerdict,
    has_contaminated_issue_body,
    has_finalized_plan_candidate,
    is_semantic_disposition_candidate,
    parse_recovered_requirements,
    parse_recovery_review,
    recovered_requirements,
    recovered_requirements_json,
    render_recovered_requirements,
    select_recovery_comment,
)
from hephaestus.automation.review_journal import HISTORY_MARKER


def _finalized_body(
    content: str = "## Why\n\nPreserve the approved behavior.",
    *,
    prefix: str = HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX,
) -> str:
    """Return a body carrying a finalized-plan marker line."""
    seal = f"R={'a' * 64} P=123456789:{'b' * 64} V=987654321:{'c' * 64} F=x -->"
    return f"{content}\n\n{prefix}{seal}"


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


@pytest.mark.parametrize(
    "prefix", [HOMERIC_INTELLIGENCE_FINALIZED_PLAN_PREFIX, ATHENA_FINALIZED_PLAN_PREFIX]
)
def test_finalized_body_is_recognized_and_not_contaminated(prefix: str) -> None:
    body = _finalized_body(prefix=prefix)

    assert has_finalized_plan_candidate(body) is True
    assert has_contaminated_issue_body(body) is False


@pytest.mark.parametrize(
    "wrap",
    [
        lambda marker: f"    {marker}",
        lambda marker: f"- example\n\n  {marker}",
        lambda marker: f"```text\n{marker}\n```",
        lambda marker: f"<div>\n{marker}\n</div>",
        lambda marker: f"Document {marker} in the guide.",
    ],
)
def test_marker_examples_are_not_top_level_finalization(wrap) -> None:
    marker = _finalized_body().splitlines()[-1]

    assert has_finalized_plan_candidate(wrap(marker)) is False


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


def test_recovered_requirements_round_trip_through_the_plain_marker() -> None:
    requirements = "## Required behavior\n\n- Keep the API stable."

    body = render_recovered_requirements(requirements)

    assert body.startswith(RECOVERED_REQUIREMENTS_MARKER)
    assert recovered_requirements(body) == requirements
    assert has_contaminated_issue_body(body) is False


def test_recovered_requirements_ignore_a_body_without_the_marker() -> None:
    assert recovered_requirements("plain issue text") is None
    assert recovered_requirements(RECOVERED_REQUIREMENTS_MARKER) is None


def test_recovery_selector_returns_the_newest_owned_recovery_comment() -> None:
    older = {"body": render_recovered_requirements("old"), "owned": True}
    foreign = {"body": render_recovered_requirements("foreign"), "owned": False}
    newest = {"body": render_recovered_requirements("new"), "owned": True}
    unrelated = {"body": "ordinary comment", "owned": True}

    selected = select_recovery_comment(
        [older, newest, foreign, unrelated],
        body_of=lambda value: str(value["body"]),
        owned_of=lambda value: bool(value["owned"]),
    )

    assert selected is newest


def test_recovery_selector_returns_none_without_an_owned_recovery_comment() -> None:
    assert (
        select_recovery_comment(
            [{"body": render_recovered_requirements("x"), "owned": False}],
            body_of=lambda value: str(value["body"]),
            owned_of=lambda value: bool(value["owned"]),
        )
        is None
    )


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
