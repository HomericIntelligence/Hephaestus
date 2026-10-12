"""Bounded compacted finding history without transport dependencies.

Data hygiene in this module is per item. One malformed compacted identity or
one malformed retained record is quarantined, and the host reports a warning
that names its recovery action. The usable remainder keeps the work item
alive.

The checks that stay all-or-nothing protect the durable state and the GitHub
comment transport. Each retained check states its one-line reason in the
docstring of the function that owns it.
"""

from __future__ import annotations

import json
import re
from collections.abc import MutableSequence
from html import escape
from typing import TypedDict

from .review_anchors import (
    MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS,
    _full_sha,
    normalize_review_finding_records,
)
from .review_warnings import ReviewWarning, record_review_warning

MAX_COMPACTED_REVIEW_FINDINGS = 256
MAX_REVIEW_FINDING_COLLECTION_BYTES = 40_000
MAX_REVIEW_FINDING_BATCH_BYTES = 24_000
MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS = 39_000
_TERMINAL_FINDING_OUTCOMES = ("corrected", "not_publishable", "published")
_OUTCOME_CODES = {"c": "corrected", "n": "not_publishable", "p": "published"}
_IDENTITY_RECOVERY = (
    "Clear the hidden review finding journal comment, then re-run the review round "
    "so the host rebuilds the compacted finding history."
)
_RECORD_RECOVERY = (
    "Clear the hidden review finding journal comment, then re-run the review round "
    "so the host rebuilds the retained finding records."
)


class ReviewFindingCompactedOutcomes(TypedDict):
    """Bounded identities and exact terminal finding counts."""

    counts: dict[str, int]
    identities: list[list[str]]


def _normalized_compacted_identity(identity: object) -> list[str] | None:
    """Return one usable compacted identity, or None when it is malformed.

    Args:
        identity: One candidate compacted identity.

    Returns:
        The normalized identity, or None when the host cannot read it.

    """
    if not isinstance(identity, (list, tuple)) or len(identity) != 4:
        return None
    finding_id, source_head, outcome_code, blocking_code = identity
    if (
        not isinstance(finding_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", finding_id) is None
        or not isinstance(source_head, str)
        or not _full_sha(source_head)
        or not isinstance(outcome_code, str)
        or outcome_code not in _OUTCOME_CODES
        or not isinstance(blocking_code, str)
        or blocking_code not in {"a", "b"}
    ):
        return None
    return [finding_id, source_head, outcome_code, blocking_code]


def _quarantine_identity(warnings: MutableSequence[ReviewWarning] | None, index: int) -> None:
    """Report one compacted identity that the host cannot use."""
    record_review_warning(
        warnings,
        "finding_identity_quarantined",
        message=(f"Compacted review finding identity {index} is invalid. The host skipped it."),
        recovery=_IDENTITY_RECOVERY,
    )


def _usable_finding_records(
    records: object,
    warnings: MutableSequence[ReviewWarning] | None,
) -> tuple[dict[str, object], ...] | None:
    """Return the usable records of one collection, or None for a whole failure.

    The host validates each record on its own. A record that cannot be read is
    quarantined. The host then validates the survivors as one collection, so a
    duplicate finding identity or an aggregate limit still fails closed.

    Args:
        records: Candidate retained finding records.
        warnings: Collector that the caller reads, or None to log.

    Returns:
        The usable records, or None when the failure was not per item.

    """
    if not isinstance(records, (list, tuple)):
        return None
    usable: list[dict[str, object]] = []
    quarantined: list[int] = []
    for index, record in enumerate(records):
        try:
            usable.extend(normalize_review_finding_records([record]))
        except ValueError:
            quarantined.append(index)
    if not quarantined:
        return None
    # Re-validate the survivors. A duplicate finding identity means the journal
    # lost track of which record it holds, so it keeps raising here.
    normalized = normalize_review_finding_records(usable)
    for index in quarantined:
        record_review_warning(
            warnings,
            "finding_record_quarantined",
            message=f"Retained review finding record {index} is invalid. The host skipped it.",
            recovery=_RECORD_RECOVERY,
        )
    return normalized


def normalize_review_finding_batch_records(
    records: object,
) -> tuple[dict[str, object], ...]:
    """Normalize a new finding batch within its reserved transport space.

    This intake stays all-or-nothing. It validates a new reviewer batch before
    the host publishes it, and a partial batch would look complete while it
    publishes fewer findings than the reviewer sent. The aggregate byte and
    public rendering limits also stay all-or-nothing because they map to the
    GitHub comment transport limit.

    Raises:
        ValueError: If any record is invalid or the batch exceeds a size limit.

    """
    normalized = normalize_review_finding_records(records)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) > MAX_REVIEW_FINDING_BATCH_BYTES:
        raise ValueError("review finding batch exceeds its aggregate size limit")
    visible_lines = ["## Retained review findings"]
    visible_lines.extend(
        "- "
        f"`{record['status']}` `{record['severity']}` from "
        f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
        for record in normalized
    )
    encoded_payload_chars = ((len(encoded) + 2) // 3) * 4
    public_section_chars = (
        len("\n\n" + "\n".join(visible_lines) + "\n\n")
        + len("<!-- hephaestus-review-finding-records: -->")
        + encoded_payload_chars
    )
    if public_section_chars > MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS:
        raise ValueError("review finding batch exceeds its public rendering limit")
    return normalized


def empty_review_finding_compacted_outcomes() -> ReviewFindingCompactedOutcomes:
    """Return one normalized empty compacted-outcome collection."""
    return {
        "counts": dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0),
        "identities": [],
    }


def normalize_review_finding_compacted_outcomes(
    value: object,
    *,
    retained_finding_ids: object = (),
    warnings: MutableSequence[ReviewWarning] | None = None,
) -> ReviewFindingCompactedOutcomes:
    """Validate bounded terminal outcomes that no longer need full records.

    One malformed compacted identity is quarantined. The host warns about it and
    keeps the usable identities. The declared outcome counts are derived state,
    so the host recomputes them from the surviving identities.

    These checks stay all-or-nothing. Each one protects the durable state:

    - The envelope shape stays all-or-nothing because the counts and identities
      have no meaning without it.
    - ``retained_finding_ids`` stays all-or-nothing because a retained identity
      that the host cannot read proves nothing about which record it holds.
    - The identity count stays all-or-nothing because it bounds the durable
      collection size.
    - A duplicated or already retained identity stays all-or-nothing because the
      journal would lose track of which terminal record it holds.
    - Counts that disagree with the identities stay all-or-nothing because the
      journal cannot tell a lost identity from a corrupt summary.

    Args:
        value: Candidate compacted outcomes.
        retained_finding_ids: Identities that the retained records hold.
        warnings: Collector that the caller reads, or None to log.

    Returns:
        The usable compacted outcomes.

    Raises:
        ValueError: If the envelope, the retained identities, the identity
            count, a duplicated identity, or the declared counts are invalid.

    """
    if value is None:
        value = empty_review_finding_compacted_outcomes()
    if not isinstance(value, dict) or set(value) != {"counts", "identities"}:
        raise ValueError("review finding compacted outcomes are invalid")
    raw_counts = value.get("counts")
    raw_identities = value.get("identities")
    if (
        not isinstance(raw_counts, dict)
        or set(raw_counts) != set(_TERMINAL_FINDING_OUTCOMES)
        or not isinstance(raw_identities, (list, tuple))
        or len(raw_identities) > MAX_COMPACTED_REVIEW_FINDINGS
    ):
        raise ValueError("review finding compacted outcomes are invalid")
    if not isinstance(retained_finding_ids, (list, tuple, set)) or not all(
        isinstance(finding_id, str) and re.fullmatch(r"[0-9a-f]{64}", finding_id)
        for finding_id in retained_finding_ids
    ):
        raise ValueError("review finding retained identities are invalid")
    retained_ids = set(retained_finding_ids)
    counts: dict[str, int] = {}
    for outcome in _TERMINAL_FINDING_OUTCOMES:
        count = raw_counts.get(outcome)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError("review finding compacted outcome counts are invalid")
        counts[outcome] = count
    identities: list[list[str]] = []
    compacted_ids: set[str] = set()
    actual_counts = dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0)
    for index, identity in enumerate(raw_identities):
        normalized = _normalized_compacted_identity(identity)
        if normalized is None:
            _quarantine_identity(warnings, index)
            continue
        finding_id, _source_head, outcome_code, _blocking_code = normalized
        if finding_id in compacted_ids or finding_id in retained_ids:
            raise ValueError("review finding compacted identity is invalid")
        identities.append(normalized)
        compacted_ids.add(finding_id)
        actual_counts[_OUTCOME_CODES[outcome_code]] += 1
    if len(identities) != len(raw_identities):
        # A quarantined identity has no usable outcome, so the declared summary
        # no longer describes the survivors.
        counts = dict(actual_counts)
    elif counts != actual_counts:
        raise ValueError("review finding compacted outcome counts do not match identities")
    return {"counts": counts, "identities": identities}


def normalize_review_finding_collection(
    findings: object,
    *,
    compacted_outcomes: object = None,
    warnings: MutableSequence[ReviewWarning] | None = None,
) -> tuple[tuple[dict[str, object], ...], ReviewFindingCompactedOutcomes]:
    """Normalize one versioned or legacy cumulative finding history.

    One malformed retained record is quarantined. The host warns about it and
    keeps the usable records.

    These checks stay all-or-nothing. Each one protects the durable state:

    - Duplicate compacted state stays all-or-nothing because the host cannot
      tell which of the two states it would overwrite.
    - The envelope shape and its format version stay all-or-nothing because a
      different version has a different durable meaning.
    - A collection version that conflicts with a record field stays
      all-or-nothing because the version would misstate the durable history.
    - The aggregate byte and public rendering limits stay all-or-nothing
      because they map to the GitHub comment transport limit.

    Args:
        findings: Candidate cumulative finding history.
        compacted_outcomes: Compacted outcomes for a separate legacy list.
        warnings: Collector that the caller reads, or None to log.

    Returns:
        The usable retained records and their compacted outcomes.

    Raises:
        ValueError: If the envelope, the format version, the compacted state, or
            an aggregate size limit is invalid.

    """
    collection_format: int | None = None
    legacy_list = not isinstance(findings, dict) and compacted_outcomes is None
    if isinstance(findings, dict):
        if compacted_outcomes is not None:
            raise ValueError("review finding collection has duplicate compacted state")
        raw_format = findings.get("format")
        if (
            set(findings) != {"format", "findings", "compacted_outcomes"}
            or type(raw_format) is not int
            or raw_format not in {1, 2}
        ):
            raise ValueError("review finding collection is invalid")
        collection_format = raw_format
        compacted_outcomes = findings.get("compacted_outcomes")
        findings = findings.get("findings")
    try:
        records = normalize_review_finding_records(findings)
    except ValueError:
        usable = _usable_finding_records(findings, warnings)
        if usable is None:
            raise
        records = usable
    if collection_format == 1 and any("publication_head" in record for record in records):
        raise ValueError("review finding collection version is invalid")
    compacted = normalize_review_finding_compacted_outcomes(
        compacted_outcomes,
        retained_finding_ids=[record["finding_id"] for record in records],
        warnings=warnings,
    )
    # Legacy lists were accepted against their list-only transport bounds.
    # Envelope expansion must not make a previously valid read fail. All new
    # writes call review_finding_collection_payload, which validates format 1
    # or format 2 after it adds the envelope.
    if legacy_list:
        return records, compacted
    payload: dict[str, object] = {
        "format": 2 if any("publication_head" in record for record in records) else 1,
        "findings": records,
        "compacted_outcomes": compacted,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    encoded_bytes = encoded.encode("utf-8")
    if len(encoded_bytes) > MAX_REVIEW_FINDING_COLLECTION_BYTES:
        raise ValueError("review finding collection exceeds its aggregate size limit")
    counts = compacted["counts"]
    visible_lines = ["## Retained review findings"]
    if compacted["identities"]:
        visible_lines.append(
            "Earlier outcomes: "
            f"published {counts['published']}, corrected {counts['corrected']}, "
            f"not publishable {counts['not_publishable']}"
        )
    visible_lines.extend(
        "- "
        f"`{record['status']}` `{record['severity']}` from "
        f"`{record['source_head']}`: {escape(str(record['body']), quote=False)}"
        for record in records
    )
    encoded_payload_chars = ((len(encoded_bytes) + 2) // 3) * 4
    public_section_chars = (
        len("\n\n" + "\n".join(visible_lines) + "\n\n")
        + len("<!-- hephaestus-review-finding-records: -->")
        + encoded_payload_chars
    )
    if public_section_chars > MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS:
        raise ValueError("review finding collection exceeds its public rendering limit")
    return records, compacted


def review_finding_compacted_outcomes_leave_batch_capacity(value: object) -> bool:
    """Return whether compact state leaves space for one maximum review batch."""
    compacted = normalize_review_finding_compacted_outcomes(value)
    if not compacted["identities"]:
        return True
    payload = {
        "format": 1,
        "findings": [],
        "compacted_outcomes": compacted,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope_bytes = len(encoded) - len(b"[]")
    # A publication head replaces prior state for the same finding identity.
    # The removed retained record or compact identity is larger than this field.
    if MAX_REVIEW_FINDING_BATCH_BYTES + envelope_bytes > MAX_REVIEW_FINDING_COLLECTION_BYTES:
        return False
    counts = compacted["counts"]
    outcome_line = (
        "Earlier outcomes: "
        f"published {counts['published']}, corrected {counts['corrected']}, "
        f"not publishable {counts['not_publishable']}"
    )
    encoded_growth_bound = ((envelope_bytes + 2) // 3) * 4
    return (
        MAX_REVIEW_FINDING_BATCH_PUBLIC_SECTION_CHARS + encoded_growth_bound + len(outcome_line) + 1
        <= MAX_REVIEW_FINDING_PUBLIC_SECTION_CHARS
    )


def review_finding_collection_payload(
    findings: object,
    compacted_outcomes: object = None,
    *,
    warnings: MutableSequence[ReviewWarning] | None = None,
) -> dict[str, object]:
    """Return the normalized JSON form of one cumulative finding history.

    The write stays all-or-nothing because the host writes this payload as the
    durable journal record. A partial payload would replace the durable history
    with an incomplete one that later reads accept as complete.
    """
    records, compacted = normalize_review_finding_collection(
        findings,
        compacted_outcomes=compacted_outcomes,
        warnings=warnings,
    )
    payload: dict[str, object] = {
        "format": 2 if any("publication_head" in record for record in records) else 1,
        "findings": [dict(record) for record in records],
        "compacted_outcomes": compacted,
    }
    normalize_review_finding_collection(payload)
    return payload


def compact_terminal_review_finding_collection(
    findings: object,
    compacted_outcomes: object = None,
    *,
    proven_outcomes: object = None,
    warnings: MutableSequence[ReviewWarning] | None = None,
) -> tuple[tuple[dict[str, object], ...], ReviewFindingCompactedOutcomes]:
    """Compact terminal records until one versioned collection fits.

    The proven outcomes and the compaction loop stay all-or-nothing. The loop
    removes one proven terminal record at a time, so a partial loop result
    would compact a record that the host cannot prove.
    """
    raw_compacted = normalize_review_finding_compacted_outcomes(
        compacted_outcomes, warnings=warnings
    )
    if not isinstance(findings, dict):
        records, _legacy_compacted = normalize_review_finding_collection(
            findings, warnings=warnings
        )
        compacted = normalize_review_finding_compacted_outcomes(
            raw_compacted,
            retained_finding_ids=[record["finding_id"] for record in records],
            warnings=warnings,
        )
    else:
        records, compacted = normalize_review_finding_collection(
            findings,
            compacted_outcomes=compacted_outcomes,
            warnings=warnings,
        )
    retained = [dict(record) for record in records]
    if proven_outcomes is not None:
        if not isinstance(proven_outcomes, dict) or not all(
            isinstance(finding_id, str)
            and re.fullmatch(r"[0-9a-f]{64}", finding_id) is not None
            and outcome in _TERMINAL_FINDING_OUTCOMES
            for finding_id, outcome in proven_outcomes.items()
        ):
            raise ValueError("review finding proven outcomes are invalid")
        pending_ids = {
            str(record["finding_id"]) for record in retained if record["status"] == "pending"
        }
        if not set(proven_outcomes).issubset(pending_ids):
            raise ValueError("review finding proven outcomes are invalid")
        retained = [
            {
                **record,
                "status": proven_outcomes.get(str(record["finding_id"]), record["status"]),
            }
            for record in retained
        ]
    identities = [list(identity) for identity in compacted["identities"]]
    outcome_codes = {"corrected": "c", "not_publishable": "n", "published": "p"}
    while True:
        counts = dict.fromkeys(_TERMINAL_FINDING_OUTCOMES, 0)
        for identity in identities:
            counts[_OUTCOME_CODES[identity[2]]] += 1
        compacted = normalize_review_finding_compacted_outcomes(
            {"counts": counts, "identities": identities},
            retained_finding_ids=[record["finding_id"] for record in retained],
            warnings=warnings,
        )
        try:
            return normalize_review_finding_collection(
                retained,
                compacted_outcomes=compacted,
                warnings=warnings,
            )
        except ValueError:
            candidate_index = next(
                (index for index, record in enumerate(retained) if record["status"] != "pending"),
                None,
            )
            if candidate_index is None:
                raise ValueError(
                    "review finding collection has no compactable terminal record"
                ) from None
            candidate = retained.pop(candidate_index)
            identities.append(
                [
                    str(candidate["finding_id"]),
                    str(candidate["source_head"]),
                    outcome_codes[str(candidate["status"])],
                    "b" if candidate["severity"] in {"critical", "major"} else "a",
                ]
            )
