"""Retained finding provenance and capacity for one review stage."""

from hephaestus.automation.review_anchors import (
    MAX_REVIEW_FINDINGS,
    normalize_review_finding_records,
)
from hephaestus.automation.review_finding_history import (
    MAX_COMPACTED_REVIEW_FINDINGS,
    ReviewFindingCompactedOutcomes,
    empty_review_finding_compacted_outcomes,
    normalize_review_finding_collection,
    normalize_review_finding_compacted_outcomes,
    review_finding_compacted_outcomes_leave_batch_capacity,
)

from .base import WorkItem


def _effective_recovered_review_finding_records(
    item: WorkItem,
) -> list[dict[str, object]]:
    """Return recovered records that can apply to the current review head."""
    recovered = list(
        normalize_review_finding_records(item.payload.get("carried_review_finding_records", []))
    )
    if item.payload.get("review_finding_journal_head") != item.payload.get("reviewed_pr_head_sha"):
        recovered = [record for record in recovered if record["status"] != "pending"]
    return recovered


def _publication_head(record: dict[str, object]) -> object:
    """Return the exact head that owns one pending publication."""
    return record.get("publication_head", record.get("source_head"))


def _restore_review_finding_provenance(
    record: dict[str, object],
    prior: dict[str, object] | None,
    compacted_prior: list[str] | None,
) -> dict[str, object]:
    """Restore first-source provenance for one current finding."""
    value = dict(record)
    publication_head = _publication_head(value)
    if prior is not None:
        value["source_head"] = prior["source_head"]
        if prior["status"] == "pending" and value["surface"] == "inline":
            value["status"] = "pending"
    elif compacted_prior is not None:
        value["source_head"] = compacted_prior[1]
    if publication_head != value["source_head"]:
        value["publication_head"] = publication_head
    else:
        value.pop("publication_head", None)
    return value


def _pending_review_finding(record: dict[str, object]) -> dict[str, object]:
    """Restore one pending finding from its saved exact anchor."""
    anchor = record["final_anchor"]
    if not isinstance(anchor, dict):
        raise ValueError("pending review finding has no final anchor")
    finding: dict[str, object] = {
        "finding_id": record["finding_id"],
        "path": anchor["path"],
        "line": anchor["line"],
        "side": anchor["side"],
        "severity": record["severity"],
        "body": record["body"],
    }
    for key in ("evidence", "scope_retraction_paths"):
        if key in record:
            finding[key] = record[key]
    return finding


def _review_finding_history_has_capacity(item: WorkItem) -> bool:
    """Prepare effective history and reserve one maximum reviewer response."""
    recovered = _effective_recovered_review_finding_records(item)
    compacted = normalize_review_finding_compacted_outcomes(
        item.payload.get(
            "carried_review_finding_compacted_outcomes",
            empty_review_finding_compacted_outcomes(),
        ),
        retained_finding_ids=[record["finding_id"] for record in recovered],
    )

    def preserve_terminal_history() -> None:
        """Keep normalized history for terminal summary and diagnostics."""
        item.payload["review_finding_records"] = [dict(record) for record in recovered]
        item.payload["review_finding_compacted_outcomes"] = compacted

    if any(record["status"] == "pending" for record in recovered):
        preserve_terminal_history()
        return False
    projected_identities = [list(identity) for identity in compacted["identities"]]
    projected_counts = dict(compacted["counts"])
    outcome_codes = {"corrected": "c", "not_publishable": "n", "published": "p"}
    for record in recovered:
        status = str(record["status"])
        projected_counts[status] += 1
        projected_identities.append(
            [
                str(record["finding_id"]),
                str(record["source_head"]),
                outcome_codes[status],
                "b" if record["severity"] in {"critical", "major"} else "a",
            ]
        )
    if len(projected_identities) > MAX_COMPACTED_REVIEW_FINDINGS - MAX_REVIEW_FINDINGS:
        preserve_terminal_history()
        return False
    projected = normalize_review_finding_compacted_outcomes(
        {"counts": projected_counts, "identities": projected_identities}
    )
    if not review_finding_compacted_outcomes_leave_batch_capacity(projected):
        preserve_terminal_history()
        return False
    return True


def _carry_review_finding_records(
    item: WorkItem, current: list[dict[str, object]]
) -> list[dict[str, object]]:
    """Combine current outcomes with the bounded versioned review history."""
    recovered = _effective_recovered_review_finding_records(item)
    current = [dict(record) for record in normalize_review_finding_records(current)]
    compacted = normalize_review_finding_compacted_outcomes(
        item.payload.get(
            "carried_review_finding_compacted_outcomes",
            empty_review_finding_compacted_outcomes(),
        ),
        retained_finding_ids=[record["finding_id"] for record in recovered],
    )
    compacted_by_id = {identity[0]: list(identity) for identity in compacted["identities"]}
    combined = {str(record["finding_id"]): dict(record) for record in recovered}
    for record in current:
        finding_id = str(record["finding_id"])
        prior = combined.get(finding_id)
        compacted_prior = compacted_by_id.pop(finding_id, None)
        value = _restore_review_finding_provenance(record, prior, compacted_prior)
        if prior is not None:
            combined.pop(finding_id)
        combined[finding_id] = value
    current_ids = {str(record["finding_id"]) for record in current}
    protected_ids = current_ids | {
        finding_id for finding_id, record in combined.items() if record["status"] == "pending"
    }
    outcome_codes = {"corrected": "c", "not_publishable": "n", "published": "p"}

    def compacted_value() -> ReviewFindingCompactedOutcomes:
        """Return normalized counts for the current compact identities."""
        identities = list(compacted_by_id.values())
        counts = {"corrected": 0, "not_publishable": 0, "published": 0}
        outcome_by_code = {
            "c": "corrected",
            "n": "not_publishable",
            "p": "published",
        }
        for identity in identities:
            counts[outcome_by_code[identity[2]]] += 1
        return normalize_review_finding_compacted_outcomes(
            {"counts": counts, "identities": identities},
            retained_finding_ids=list(combined),
        )

    while True:
        records = list(combined.values())
        try:
            normalized_records, normalized_compacted = normalize_review_finding_collection(
                records,
                compacted_outcomes=compacted_value(),
            )
            if len(normalized_records) <= MAX_REVIEW_FINDINGS:
                break
        except ValueError:
            normalized_records = ()
        candidate_id = next(
            (
                finding_id
                for finding_id, record in combined.items()
                if finding_id not in protected_ids and record["status"] != "pending"
            ),
            None,
        )
        if candidate_id is None:
            raise ValueError("review finding history has no compactable terminal record")
        candidate = combined.pop(candidate_id)
        if candidate_id in compacted_by_id:
            raise ValueError("review finding compacted identity is duplicated")
        compacted_by_id[candidate_id] = [
            candidate_id,
            str(candidate["source_head"]),
            outcome_codes[str(candidate["status"])],
            "b" if candidate["severity"] in {"critical", "major"} else "a",
        ]
    item.payload["review_finding_compacted_outcomes"] = normalized_compacted
    return [dict(record) for record in normalized_records]
