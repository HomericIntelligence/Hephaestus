"""Store rebase evidence for fresh host verification after a restart."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .implementation_go_audit_receipt import (
    parse_pending_implementation_go_audit,
    render_pending_implementation_go_audit,
)
from .review_audit import ReviewAudit

REBASE_REVIEW_PREFIX = "<!-- hephaestus-review-rebase:"
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_FIELDS = (
    "repository",
    "issue_number",
    "pr_number",
    "reviewed_head_sha",
    "reviewed_base_sha",
    "source_head_sha",
    "target_base_sha",
    "resulting_head_sha",
    "resulting_tree_sha",
    "original_audit_id",
    "state",
)


def original_audit_identity(pr_number: int, reviewed_head_sha: str) -> str:
    """Return the public audit marker for the original reviewed commit."""
    return f"<!-- hephaestus-implementation-go-audit:pr={pr_number}:head={reviewed_head_sha} -->"


@dataclass(frozen=True)
class RebaseReviewRecord:
    """Retain review identity and rebase facts without merge authority."""

    repository: str
    issue_number: int
    pr_number: int
    reviewed_head_sha: str
    reviewed_base_sha: str
    source_head_sha: str
    target_base_sha: str
    resulting_head_sha: str
    resulting_tree_sha: str
    original_audit_id: str
    audit: ReviewAudit
    state: str = "active"

    def __post_init__(self) -> None:
        """Reject incomplete identities and invalid audit records."""
        if (
            not isinstance(self.repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository) is None
        ):
            raise ValueError("rebase record repository is invalid")
        for name in ("issue_number", "pr_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("rebase record identifier is invalid")
        for name in _FIELDS:
            if name.endswith("_sha"):
                value = getattr(self, name)
                if not isinstance(value, str) or _SHA.fullmatch(value) is None:
                    raise ValueError("rebase record commit is invalid")
        if self.state not in {"active", "revoked"} or self.original_audit_id != (
            original_audit_identity(self.pr_number, self.reviewed_head_sha)
        ):
            raise ValueError("rebase record audit identity is invalid")
        render_pending_implementation_go_audit(self.pr_number, self.reviewed_head_sha, self.audit)


def render_review_rebase_record(record: RebaseReviewRecord) -> tuple[str, str]:
    """Render a bounded record that does not contain a process proof."""
    marker = f"{REBASE_REVIEW_PREFIX}v1:pr={record.pr_number} -->"
    _, audit_body = render_pending_implementation_go_audit(
        record.pr_number, record.reviewed_head_sha, record.audit
    )
    payload = {name: getattr(record, name) for name in _FIELDS}
    payload["audit"] = audit_body
    return marker, marker + "\n" + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("rebase record has duplicate fields")
        result[key] = value
    return result


def parse_review_rebase_record(body: str) -> RebaseReviewRecord | None:
    """Reject malformed records and preserve an explicit revoked state."""
    if not body.startswith(REBASE_REVIEW_PREFIX):
        return None
    marker, separator, raw = body.partition("\n")
    if not separator or len(body) > 24000:
        raise ValueError("rebase record is malformed")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(payload, dict) or set(payload) != {*_FIELDS, "audit"}:
        raise ValueError("rebase record fields are invalid")
    audit_body = payload.pop("audit")
    if not isinstance(audit_body, str):
        raise ValueError("rebase record audit is invalid")
    audit = parse_pending_implementation_go_audit(audit_body)
    if (
        audit is None
        or audit.pr_number != payload["pr_number"]
        or (audit.head_sha != payload["reviewed_head_sha"])
    ):
        raise ValueError("rebase record audit does not match")
    record = RebaseReviewRecord(**payload, audit=audit.audit)
    if marker != f"{REBASE_REVIEW_PREFIX}v1:pr={record.pr_number} -->":
        raise ValueError("rebase record marker does not match")
    return record
