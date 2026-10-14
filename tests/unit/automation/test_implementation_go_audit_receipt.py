"""Behavior tests for durable implementation-GO audit receipts."""

from __future__ import annotations

import base64
import json

import pytest

from hephaestus.automation import implementation_go_audit_receipt as audit_receipts
from hephaestus.automation.github_api.diff import (
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_batch_records,
    normalize_review_finding_collection,
    normalize_review_finding_records,
    review_finding_collection_payload,
)
from hephaestus.automation.implementation_go_audit_receipt import (
    LegacyPendingImplementationGoAuditError,
    PendingImplementationGoAudit,
    parse_pending_implementation_go_audit,
    parse_published_implementation_go_audit,
    render_pending_implementation_go_audit,
)
from hephaestus.automation.review_anchors import ReviewFindingSizeError
from hephaestus.automation.review_audit import ReviewAudit, render_implementation_go_audit
from hephaestus.automation.review_warnings import ReviewWarning


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


def _finding_record(*, status: str = "not_publishable") -> dict[str, object]:
    """Return one bounded journal record."""
    return {
        "finding_id": "f" * 64,
        "source_head": "a" * 40,
        "severity": "minor",
        "body": "Preserve this advisory finding.",
        "evidence": "The worker does not receive the required state.",
        "original_anchor": {"path": "old.py", "line": 99, "side": "RIGHT"},
        "final_anchor": None,
        "status": status,
        "surface": "audit" if status != "not_publishable" else "not_publishable",
        "reason": "line_not_in_diff",
    }


def test_review_finding_journal_round_trip_is_exact_head_and_versioned() -> None:
    """The durable journal preserves one complete bounded finding record."""
    records = (_finding_record(),)

    marker, body = audit_receipts.render_review_finding_journal(7, "a" * 40, records)
    journal = audit_receipts.parse_review_finding_journal(body)

    assert marker == f"<!-- hephaestus-review-findings:pr=7:head={'a' * 40} -->"
    assert journal is not None
    assert journal.pr_number == 7
    assert journal.head_sha == "a" * 40
    assert journal.finding_records == records


def test_review_finding_journal_preserves_separate_publication_head() -> None:
    """The journal keeps first-source provenance and pending publication identity."""
    record = {**_finding_record(status="published"), "publication_head": "b" * 40}

    _marker, body = audit_receipts.render_review_finding_journal(7, "b" * 40, (record,))
    journal = audit_receipts.parse_review_finding_journal(body)
    payload = json.loads(body.split("\n", 1)[1].removeprefix("<!-- ").removesuffix(" -->"))

    assert journal is not None
    assert journal.finding_records == (record,)
    assert payload["collection"]["format"] == 2


def test_review_finding_journal_round_trip_preserves_compacted_outcomes() -> None:
    """The versioned journal preserves bounded identities for compacted outcomes."""
    compacted = {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 1},
        "identities": [["e" * 64, "9" * 40, "p", "a"]],
    }

    _marker, body = audit_receipts.render_review_finding_journal(
        7,
        "a" * 40,
        (_finding_record(status="corrected"),),
        compacted_outcomes=compacted,
    )
    journal = audit_receipts.parse_review_finding_journal(body)

    assert journal is not None
    assert journal.compacted_outcomes == compacted


def test_pending_and_public_audits_preserve_compacted_outcomes() -> None:
    """GO audit recovery keeps compacted identities through public publication."""
    records = (_finding_record(status="corrected"),)
    compacted = {
        "counts": {"corrected": 0, "not_publishable": 1, "published": 0},
        "identities": [["e" * 64, "9" * 40, "n", "a"]],
    }

    _pending_marker, pending_body = render_pending_implementation_go_audit(
        7,
        "b" * 40,
        _clean_audit(),
        finding_records=records,
        compacted_outcomes=compacted,
    )
    pending = parse_pending_implementation_go_audit(pending_body)
    _public_marker, public_body = render_implementation_go_audit(
        _clean_audit(),
        pr_number=7,
        head_sha="b" * 40,
        finding_records=records,
        compacted_outcomes=compacted,
    )
    public = parse_published_implementation_go_audit(public_body)

    assert pending is not None
    assert pending.compacted_outcomes == compacted
    assert public is not None
    assert public.compacted_outcomes == compacted
    assert "Earlier outcomes: published 0, corrected 0, not publishable 1" in public_body


@pytest.mark.parametrize(
    "compacted",
    [
        {
            "counts": {"corrected": 0, "not_publishable": 0, "published": 2},
            "identities": [["e" * 64, "9" * 40, "p", "a"]],
        },
        {
            "counts": {"corrected": 0, "not_publishable": 0, "published": 1},
            "identities": [["f" * 64, "9" * 40, "p", "a"]],
        },
    ],
    ids=("count-mismatch", "retained-duplicate"),
)
def test_review_finding_collection_rejects_invalid_compacted_state(
    compacted: dict[str, object],
) -> None:
    """A journal rejects inexact or duplicate compact identities.

    The host cannot tell which of two conflicting durable states it would
    overwrite, so that check stays all-or-nothing.
    """
    with pytest.raises(ValueError, match="review finding"):
        audit_receipts.render_review_finding_journal(
            7,
            "a" * 40,
            (_finding_record(status="corrected"),),
            compacted_outcomes=compacted,
        )


def test_review_finding_collection_quarantines_one_malformed_compact_identity() -> None:
    """One malformed compact identity is quarantined and reported.

    A pending outcome has no durable meaning for a compacted identity. The
    host drops that identity, warns about it, and keeps the usable records.
    """
    compacted = {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 1},
        "identities": [["e" * 64, "9" * 40, "pending", "a"]],
    }

    warnings: list[ReviewWarning] = []
    records, retained = normalize_review_finding_collection(
        (_finding_record(status="corrected"),),
        compacted_outcomes=compacted,
        warnings=warnings,
    )

    assert len(records) == 1
    assert retained == {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 0},
        "identities": [],
    }
    assert [warning.code for warning in warnings] == ["finding_identity_quarantined"]
    assert all(warning.recovery for warning in warnings)


def test_legacy_review_finding_payloads_normalize_without_compacted_state() -> None:
    """Old journal and public payloads remain valid with empty compacted history."""
    record = _finding_record(status="corrected")
    journal_marker = f"<!-- hephaestus-review-findings:pr=7:head={'a' * 40} -->"
    journal_payload = json.dumps(
        {
            "format": 1,
            "pr_number": 7,
            "head_sha": "a" * 40,
            "findings": [record],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    journal = audit_receipts.parse_review_finding_journal(
        f"{journal_marker}\n<!-- {journal_payload} -->"
    )
    _base_marker, base_body = render_implementation_go_audit(
        _clean_audit(), pr_number=7, head_sha="a" * 40
    )
    encoded = base64.urlsafe_b64encode(
        json.dumps([record], sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    public_body = (
        f"{base_body}\n\n## Retained review findings\n"
        f"- `corrected` `minor` from `{'a' * 40}`: Preserve this advisory finding.\n\n"
        f"<!-- hephaestus-review-finding-records:{encoded} -->"
    )
    public = parse_published_implementation_go_audit(public_body)

    assert journal is not None
    assert journal.compacted_outcomes == {
        "counts": {"corrected": 0, "not_publishable": 0, "published": 0},
        "identities": [],
    }
    assert public is not None
    assert public.compacted_outcomes == journal.compacted_outcomes


def test_legacy_list_keeps_its_original_public_rendering_boundary() -> None:
    """A valid legacy list remains readable when envelope metadata would exceed its limit."""
    low = 1
    high = 16_384
    largest_legacy: dict[str, object] | None = None
    while low <= high:
        size = (low + high) // 2
        candidate = {
            **_finding_record(status="corrected"),
            "body": "<" * size,
            "evidence": "e" * 1_000,
        }
        try:
            normalize_review_finding_records([candidate])
        except ValueError:
            high = size - 1
        else:
            largest_legacy = candidate
            low = size + 1

    assert largest_legacy is not None
    records, compacted = normalize_review_finding_collection([largest_legacy])
    assert records == (largest_legacy,)
    assert compacted == empty_review_finding_compacted_outcomes()
    with pytest.raises(ValueError, match="public rendering"):
        review_finding_collection_payload([largest_legacy])
    compact_payload = review_finding_collection_payload(
        [],
        {
            "counts": {"corrected": 1, "not_publishable": 0, "published": 0},
            "identities": [["f" * 64, "a" * 40, "c", "a"]],
        },
    )
    assert compact_payload["findings"] == []


def test_review_finding_journal_quarantines_an_unknown_status_record() -> None:
    """A journal quarantines one record whose values the host cannot read.

    One corrupt retained record is quarantined. The host warns about it and
    keeps the usable records. The envelope, the format version, and the
    compacted state stay all-or-nothing.
    """
    unknown = {**_finding_record(), "status": "pending"}
    usable = {**_finding_record(), "finding_id": "e" * 64}

    warnings: list[ReviewWarning] = []
    normalized, _ = normalize_review_finding_collection((unknown, usable), warnings=warnings)

    assert [record["finding_id"] for record in normalized] == ["e" * 64]
    assert [warning.code for warning in warnings] == ["finding_record_quarantined"]
    assert all(warning.recovery for warning in warnings)
    _marker, body = audit_receipts.render_review_finding_journal(7, "a" * 40, (unknown, usable))
    assert body.count("hephaestus-review-findings") == 1


def test_review_finding_journal_refuses_an_oversized_record_instead_of_quarantine() -> None:
    """An oversized record raises so the host can compact it.

    A size failure is not corrupt data. The host compacts an oversized record
    into a bounded identity. The host must never drop that record.
    """
    oversized = {**_finding_record(), "body": "x" * 20_000}

    warnings: list[ReviewWarning] = []
    with pytest.raises(ReviewFindingSizeError):
        normalize_review_finding_collection((oversized,), warnings=warnings)

    assert warnings == []


@pytest.mark.parametrize("status", ["corrected", "not_publishable"])
def test_review_finding_journal_quarantines_a_corrected_outcome_without_reason(
    status: str,
) -> None:
    """A corrected outcome without its typed reason is quarantined.

    The record does not name why the host corrected or withheld it, so the
    host cannot explain the outcome to the reviewer. It reports the record and
    keeps the usable records.
    """
    record = {**_finding_record(status=status), "reason": None}

    warnings: list[ReviewWarning] = []
    normalized, _ = normalize_review_finding_collection((record,), warnings=warnings)

    assert normalized == ()
    assert [warning.code for warning in warnings] == ["finding_record_quarantined"]
    assert all(warning.recovery for warning in warnings)


def test_review_finding_journal_preserves_normalized_source_scope() -> None:
    """A durable finding record keeps its complete normalized source scope."""
    record = {
        **_finding_record(status="corrected"),
        "scope_retraction_paths": ["z.py", "a.py", "z.py"],
    }

    _marker, body = audit_receipts.render_review_finding_journal(7, "a" * 40, (record,))
    journal = audit_receipts.parse_review_finding_journal(body)

    assert journal is not None
    assert journal.finding_records[0]["scope_retraction_paths"] == ["a.py", "z.py"]


def test_pending_go_receipt_preserves_cumulative_finding_records() -> None:
    """Publication recovery keeps advisory evidence from an earlier review head."""
    records = (_finding_record(status="corrected"),)

    _marker, body = render_pending_implementation_go_audit(
        7, "b" * 40, _clean_audit(), finding_records=records
    )
    receipt = parse_pending_implementation_go_audit(body)

    assert receipt is not None
    assert receipt.finding_records == records


def test_public_go_audit_renders_retained_advisory_source_head() -> None:
    """The public audit shows retained advisory metadata and its source head."""
    records = (_finding_record(status="corrected"),)

    _marker, body = render_implementation_go_audit(
        _clean_audit(), pr_number=7, head_sha="b" * 40, finding_records=records
    )

    assert "Preserve this advisory finding." in body
    assert "`" + "a" * 40 + "`" in body
    assert "corrected" in body
    receipt = parse_published_implementation_go_audit(body)
    assert receipt is not None
    assert receipt.finding_records == records


def test_review_finding_records_fit_all_github_comment_renderings() -> None:
    """The aggregate bound leaves room for the public base64 audit payload."""
    records = (
        {
            **_finding_record(status="corrected"),
            "body": "x" * 16_384,
            "evidence": "y" * 12_000,
        },
    )

    _journal_marker, journal = audit_receipts.render_review_finding_journal(7, "a" * 40, records)
    _pending_marker, pending = render_pending_implementation_go_audit(
        7, "a" * 40, _clean_audit(), finding_records=records
    )
    _audit_marker, public = render_implementation_go_audit(
        _clean_audit(), pr_number=7, head_sha="a" * 40, finding_records=records
    )

    assert len(journal) <= 65_536
    assert len(pending) <= 65_536
    assert len(public) <= 65_536


def test_review_finding_batch_rejects_oversized_utf8_aggregate() -> None:
    """The aggregate limit counts encoded bytes before base64 expansion.

    Each record in a new reviewer batch is individually bounded. Their
    aggregate is not, so the aggregate check stays all-or-nothing because it
    maps to the GitHub comment transport limit.
    """
    records = [
        {
            **_finding_record(status="corrected"),
            "finding_id": f"{index:064x}",
            "body": "é" * 8_000,
            "evidence": "e" * 2_000,
        }
        for index in range(3)
    ]

    with pytest.raises(ValueError, match="aggregate size limit"):
        normalize_review_finding_batch_records(records)


def test_review_finding_renderers_accept_bounded_utf8_records() -> None:
    """A valid multibyte record fits each GitHub comment rendering."""
    records = ({**_finding_record(status="corrected"), "body": "🙂" * 7_000},)

    _journal_marker, journal = audit_receipts.render_review_finding_journal(7, "a" * 40, records)
    _pending_marker, pending = render_pending_implementation_go_audit(
        7, "a" * 40, _clean_audit(), finding_records=records
    )
    _audit_marker, public = render_implementation_go_audit(
        _clean_audit(), pr_number=7, head_sha="a" * 40, finding_records=records
    )

    assert len(journal) <= 65_536
    assert len(pending) <= 65_536
    assert len(public) <= 65_536


def test_review_finding_batch_records_reject_oversized_visible_rendering() -> None:
    """HTML escaping cannot make an accepted public audit exceed its limit.

    Each record in a new reviewer batch is individually bounded and
    individually renderable. Their escaped aggregate is not, so the public
    rendering check stays all-or-nothing.
    """
    records = [
        {
            **_finding_record(status="corrected"),
            "finding_id": f"{index:064x}",
            "body": "&" * 5_000,
            "evidence": "y" * 500,
        }
        for index in range(3)
    ]

    with pytest.raises(ValueError, match="public rendering limit"):
        normalize_review_finding_batch_records(records)


def test_public_go_audit_with_escaped_summary_round_trips_retained_findings() -> None:
    """Rendered summary escapes do not invalidate retained finding readback."""
    audit = ReviewAudit(
        grade="A",
        summary="Value < limit & safe.",
        findings=(),
        raw_feedback="",
        valid=True,
        verdict="GO",
    )
    records = (_finding_record(status="corrected"),)

    _marker, body = render_implementation_go_audit(
        audit, pr_number=7, head_sha="b" * 40, finding_records=records
    )

    receipt = parse_published_implementation_go_audit(body)
    assert receipt is not None
    assert receipt.finding_records == records


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
        {"format": 5},
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
        "journal format is invalid" if payload in ([], {}, {"format": 5}) else "payload is invalid"
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
