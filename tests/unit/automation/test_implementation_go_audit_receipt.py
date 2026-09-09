"""Behavior tests for durable implementation-GO audit receipts."""

from __future__ import annotations

import json

import pytest

from hephaestus.automation.implementation_go_audit_receipt import (
    LegacyPendingImplementationGoAuditError,
    PendingImplementationGoAudit,
    parse_pending_implementation_go_audit,
    parse_published_implementation_go_audit,
    render_pending_implementation_go_audit,
)
from hephaestus.automation.review_audit import ReviewAudit, render_implementation_go_audit


def _clean_audit() -> ReviewAudit:
    """Return one clean structural GO audit."""
    return ReviewAudit(
        grade="A",
        summary="No blocking findings.",
        findings=(),
        raw_feedback="review output",
        valid=True,
        verdict="GO",
    )


def test_pending_audit_round_trip_preserves_bounded_evidence() -> None:
    """A valid pending receipt round-trips its exact head and audit evidence."""
    marker, body = render_pending_implementation_go_audit(7, "a" * 40, _clean_audit())
    receipt = parse_pending_implementation_go_audit(body)
    assert marker == f"<!-- hephaestus-implementation-go-audit-pending:pr=7:head={'a' * 40} -->"
    assert receipt == PendingImplementationGoAudit(7, "a" * 40, _clean_audit())


@pytest.mark.parametrize(
    ("pr_number", "head_sha"),
    [(0, "a" * 40), (-1, "a" * 40), (1, "A" * 40), (1, "a" * 39), (1, "main")],
)
def test_pending_audit_render_rejects_invalid_identity(pr_number: int, head_sha: str) -> None:
    """Pending receipt rendering requires a positive PR and a full lowercase SHA."""
    with pytest.raises(ValueError, match="identity is invalid"):
        render_pending_implementation_go_audit(pr_number, head_sha, _clean_audit())


@pytest.mark.parametrize(
    "audit",
    [
        ReviewAudit("A", "blocked", ({"body": "finding"},), "", True, "NOGO"),
        ReviewAudit(None, "clean", (), "", True, "GO"),
        ReviewAudit("A", "clean", (), "", False, "GO"),
    ],
)
def test_pending_audit_render_requires_clean_graded_go(audit: ReviewAudit) -> None:
    """Only a valid finding-free graded GO audit can enter the pending journal."""
    with pytest.raises(ValueError, match="clean GO verdict"):
        render_pending_implementation_go_audit(7, "a" * 40, audit)


def test_pending_parser_ignores_unowned_text() -> None:
    """Text without the exact pending marker is not an owned receipt."""
    assert parse_pending_implementation_go_audit("ordinary comment") is None


@pytest.mark.parametrize(
    "suffix",
    ["", "\nraw", "\n<!-- missing end", "\nmissing start -->"],
)
def test_pending_parser_rejects_malformed_payload_line(suffix: str) -> None:
    """An owned marker needs one complete comment-wrapped payload line."""
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr=7:head={'a' * 40} -->"
    with pytest.raises(ValueError, match="journal is malformed"):
        parse_pending_implementation_go_audit(marker + suffix)


def test_pending_parser_preserves_json_decode_cause() -> None:
    """Malformed JSON is translated without losing the decoder failure."""
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr=7:head={'a' * 40} -->"
    with pytest.raises(ValueError, match="journal is malformed") as raised:
        parse_pending_implementation_go_audit(f"{marker}\n<!-- {{bad}} -->")
    assert isinstance(raised.value.__cause__, json.JSONDecodeError)


def test_pending_parser_marks_legacy_receipt_for_fresh_review() -> None:
    """A format-one receipt reports its identity and requires a fresh review."""
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr=7:head={'a' * 40} -->"
    with pytest.raises(LegacyPendingImplementationGoAuditError) as raised:
        parse_pending_implementation_go_audit(f'{marker}\n<!-- {{"format":1}} -->')
    assert raised.value.pr_number == 7
    assert raised.value.head_sha == "a" * 40


def _pending_body_with_payload(payload: object, head_sha: str = "a" * 40) -> str:
    """Return a pending receipt body with caller-selected JSON payload."""
    marker = f"<!-- hephaestus-implementation-go-audit-pending:pr=7:head={head_sha} -->"
    return f"{marker}\n<!-- {json.dumps(payload)} -->"


@pytest.mark.parametrize(
    ("head_sha", "summary", "raw_feedback"),
    [("a" * 64, "s" * 200, "f" * 4000)],
    ids=("exact-maximums",),
)
def test_pending_parser_accepts_exact_boundary_values(
    head_sha: str, summary: str, raw_feedback: str
) -> None:
    """The parser accepts the maximum supported identity and text sizes."""
    payload = {
        "format": 2,
        "pr_number": 7,
        "head_sha": head_sha,
        "grade": "A",
        "verdict": "GO",
        "summary": summary,
        "raw_feedback": raw_feedback,
    }

    receipt = parse_pending_implementation_go_audit(
        _pending_body_with_payload(payload, head_sha=head_sha)
    )

    assert receipt is not None
    assert receipt.head_sha == head_sha
    assert receipt.audit.summary == summary
    assert receipt.audit.raw_feedback == raw_feedback


@pytest.mark.parametrize("head_sha", ["a" * 65], ids=("identity-too-long",))
def test_pending_parser_rejects_adjacent_overlong_identity(head_sha: str) -> None:
    """The parser does not own a marker with an overlong identity."""
    payload = {
        "format": 2,
        "pr_number": 7,
        "head_sha": head_sha,
        "grade": "A",
        "verdict": "GO",
        "summary": "clean",
        "raw_feedback": "",
    }

    assert (
        parse_pending_implementation_go_audit(
            _pending_body_with_payload(payload, head_sha=head_sha)
        )
        is None
    )


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"format": 3},
        {
            "format": 2,
            "pr_number": 8,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "clean",
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "b" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "clean",
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "Z",
            "verdict": "GO",
            "summary": "clean",
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "NOGO",
            "summary": "clean",
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "",
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": 1,
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "x" * 201,
            "raw_feedback": "",
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "clean",
            "raw_feedback": None,
        },
        {
            "format": 2,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "grade": "A",
            "verdict": "GO",
            "summary": "clean",
            "raw_feedback": "x" * 4001,
        },
    ],
    ids=(
        "non-object",
        "missing-format",
        "unknown-format",
        "wrong-pr",
        "wrong-head",
        "bad-grade",
        "bad-verdict",
        "empty-summary",
        "non-string-summary",
        "long-summary",
        "non-string-feedback",
        "long-feedback",
    ),
)
def test_pending_parser_rejects_invalid_payload_fields(payload: object) -> None:
    """Each identity, verdict, and size field is validated before recovery."""
    message = (
        "journal format is invalid" if payload in ([], {}, {"format": 3}) else "payload is invalid"
    )
    with pytest.raises(ValueError, match=message):
        parse_pending_implementation_go_audit(_pending_body_with_payload(payload))


def test_published_audit_parser_recovers_public_rendering() -> None:
    """The deterministic public comment recovers the same bounded audit identity."""
    _marker, body = render_implementation_go_audit(_clean_audit(), pr_number=7, head_sha="a" * 40)
    receipt = parse_published_implementation_go_audit(body)
    assert receipt is not None
    assert receipt.pr_number == 7
    assert receipt.head_sha == "a" * 40
    assert receipt.audit.grade == "A"
    assert receipt.audit.summary == "No blocking findings."
    assert receipt.audit.raw_feedback == ""


def test_published_audit_parser_ignores_changed_rendering() -> None:
    """A changed or partial public comment is not a deterministic audit receipt."""
    assert parse_published_implementation_go_audit("Reviewer verdict: GO") is None
