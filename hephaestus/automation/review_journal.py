"""Pure model and rendering helpers for the GitHub plan-review journal.

GitHub comments are the durable audit log, while mutually-exclusive
``state:*`` labels remain the authoritative pipeline state.  This module
keeps those roles separate and centralizes the comment format so restart,
prompt projection, and GitHub mutation code do not each invent parsers.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from hephaestus.automation.protocol import (
    PLAN_CANONICAL_MARKER,
    PLAN_COMMENT_MARKER,
    PLAN_REVIEW_CANONICAL_MARKER,
    PLAN_REVIEW_PREFIX,
)

HISTORY_MARKER: Final[str] = "<!-- hephaestus-plan-history:revision={revision}:kind={kind} -->"
HISTORY_MARKER_PREFIX: Final[str] = "<!-- hephaestus-plan-history:"
HISTORY_RE: Final[re.Pattern[str]] = re.compile(
    r"^<!-- hephaestus-plan-history:revision=(?P<revision>\d+):"
    r"kind=(?P<kind>plan|review) -->"
)
REVISION_RE: Final[re.Pattern[str]] = re.compile(r"<!-- revision: (?P<revision>\d+) -->")
PLAN_REVIEW_STATES: Final[frozenset[str]] = frozenset(
    {"state:plan-go", "state:plan-no-go", "state:plan-blocked"}
)

MAX_AGENT_HISTORY_CHARS: Final[int] = 48_000
MAX_REVIEW_SUMMARY_CHARS: Final[int] = 800
MAX_FEEDBACK_CHARS: Final[int] = 8_000

_OLD_PLAN_PAYLOAD = "<!-- hephaestus-plan-history:old-plan -->"
_NEW_PLAN_PAYLOAD = "<!-- hephaestus-plan-history:new-plan -->"
_TRUNCATION_NOTICE = (
    "<!-- hephaestus-history-projection:truncated -->\n"
    "_Older complete artifacts remain in the GitHub issue journal; this prompt "
    "contains their ordered index and the latest actionable artifacts._"
)
_TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})


@dataclass(frozen=True)
class IssueComment:
    """Issue comment metadata required for ownership and feedback decisions."""

    body: str
    author_login: str = ""
    author_association: str = ""
    created_at: str = ""
    viewer_did_author: bool = False
    database_id: int | None = None
    url: str = ""

    @property
    def is_trusted_human(self) -> bool:
        """Return whether this is maintainer feedback rather than bot output."""
        login = self.author_login.lower()
        return (
            bool(self.body.strip())
            and self.author_association.upper() in _TRUSTED_ASSOCIATIONS
            and not login.endswith("[bot]")
        )


@dataclass(frozen=True)
class HistoryArtifact:
    """One immutable superseded plan or review comment."""

    revision: int
    kind: str
    body: str


@dataclass(frozen=True)
class JournalSnapshot:
    """Current canonical artifacts plus immutable history reconstructed from GitHub."""

    revision: int
    current_plan: str
    current_review: str
    current_review_revision: int | None
    history: tuple[HistoryArtifact, ...]


def as_issue_comment(comment: IssueComment | str) -> IssueComment:
    """Coerce legacy body-only tests/callers into an automation-owned comment."""
    if isinstance(comment, IssueComment):
        return comment
    return IssueComment(body=comment, viewer_did_author=True)


def comment_body(comment: IssueComment | str) -> str:
    """Return a comment body from structured or legacy input."""
    return as_issue_comment(comment).body


def is_plan_comment(body: str) -> bool:
    """Recognize current and legacy canonical plan comments."""
    stripped = body.lstrip()
    return stripped.startswith(PLAN_CANONICAL_MARKER) or stripped.startswith(PLAN_COMMENT_MARKER)


def is_plan_review_comment(body: str) -> bool:
    """Recognize current and legacy canonical plan-review comments."""
    stripped = body.lstrip()
    return stripped.startswith(PLAN_REVIEW_CANONICAL_MARKER) or stripped.startswith(
        PLAN_REVIEW_PREFIX
    )


def is_journal_comment(body: str) -> bool:
    """Return whether the body is an automation-owned plan journal artifact."""
    stripped = body.lstrip()
    return (
        is_plan_comment(stripped)
        or is_plan_review_comment(stripped)
        or stripped.startswith(HISTORY_MARKER_PREFIX)
    )


def _without_leading_line(text: str, prefix: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith(prefix):
        return stripped
    first, separator, rest = stripped.partition("\n")
    if first.strip() != prefix:
        return stripped
    return rest if separator else ""


def _without_revision_line(text: str) -> str:
    stripped = text.lstrip()
    first, separator, rest = stripped.partition("\n")
    if REVISION_RE.fullmatch(first.strip()):
        return rest if separator else ""
    return stripped


def extract_current_plan(body: str) -> str:
    """Return only the plan payload from a current or legacy plan comment."""
    text = _without_leading_line(body, PLAN_CANONICAL_MARKER)
    text = _without_leading_line(text, PLAN_COMMENT_MARKER)
    return _without_revision_line(text).strip()


def extract_current_review(body: str) -> str:
    """Return only reviewer output from a current or legacy review comment."""
    text = _without_leading_line(body, PLAN_REVIEW_CANONICAL_MARKER)
    text = _without_leading_line(text, PLAN_REVIEW_PREFIX)
    return _without_revision_line(text).strip()


def render_current_plan(plan: str, *, revision: int = 1) -> str:
    """Render the editable current plan with an opaque canonical marker."""
    payload = extract_current_plan(plan)
    return (
        f"{PLAN_CANONICAL_MARKER}\n{PLAN_COMMENT_MARKER}\n"
        f"<!-- revision: {revision} -->\n\n{payload}"
    )


def render_current_review(review: str, *, revision: int) -> str:
    """Render the editable current review with an opaque canonical marker."""
    payload = extract_current_review(review)
    return (
        f"{PLAN_REVIEW_CANONICAL_MARKER}\n{PLAN_REVIEW_PREFIX}\n"
        f"<!-- revision: {revision} -->\n\n{payload}"
    )


def render_pending_review(*, revision: int) -> str:
    """Render the canonical review pointer before revision review completes."""
    return render_current_review(
        f"Review pending for implementation plan revision {revision}.",
        revision=revision,
    )


def is_pending_review(review: str, *, revision: int) -> bool:
    """Return whether canonical review content is the exact pending sentinel."""
    return extract_current_review(review) == (
        f"Review pending for implementation plan revision {revision}."
    )


def parse_plan_review_state(review: str) -> str | None:
    """Parse the sole final state token from a newly produced plan review.

    This parser intentionally does not recognize the historical ``Verdict:``
    vocabulary. Reviewer output selects a GitHub label only when exactly one
    supported state token is present as the final non-empty line. BLOCKED also
    requires explanatory prose so the canonical comment tells maintainers what
    input or dependency is missing.
    """
    lines = [line.strip() for line in review.splitlines() if line.strip()]
    matches = [line for line in lines if line in PLAN_REVIEW_STATES]
    if len(matches) != 1 or not lines or lines[-1] != matches[0]:
        return None
    if matches[0] == "state:plan-blocked":
        explanation = [
            line
            for line in lines[:-1]
            if not line.startswith(("#", "<!--")) and line not in PLAN_REVIEW_STATES
        ]
        if not explanation:
            return None
    return matches[0]


def comment_revision(body: str) -> int | None:
    """Read a canonical artifact's explicit revision, if present."""
    match = REVISION_RE.search(body)
    return int(match.group("revision")) if match else None


def normalized_plan(plan: str) -> str:
    """Normalize a plan for deterministic no-progress comparisons."""
    return "\n".join(line.rstrip() for line in extract_current_plan(plan).strip().splitlines())


def plan_fingerprint(plan: str) -> str:
    """Return a stable short fingerprint for a normalized plan."""
    return hashlib.sha256(normalized_plan(plan).encode("utf-8")).hexdigest()[:16]


def archive_plan_body(revision: int, old_plan: str, new_plan: str) -> str:
    """Render append-only plan history, including the next plan for crash recovery."""
    old_payload = extract_current_plan(old_plan)
    new_payload = extract_current_plan(new_plan)
    diff = (
        "\n".join(
            difflib.unified_diff(
                old_payload.splitlines(),
                new_payload.splitlines(),
                fromfile=f"Plan {revision}",
                tofile=f"Plan {revision + 1}",
                lineterm="",
            )
        )
        or "_(no textual changes)_"
    )
    marker = HISTORY_MARKER.format(revision=revision, kind="plan")
    return (
        f"{marker}\n## Previous Implementation Plan — Revision {revision}\n\n"
        f"### Changes from Revision {revision} to Revision {revision + 1}\n\n"
        f"```diff\n{diff}\n```\n\n"
        f"### Complete Plan {revision}\n\n{_OLD_PLAN_PAYLOAD}\n{old_payload}\n\n"
        f"### Recovery Payload for Plan {revision + 1}\n\n"
        f"{_NEW_PLAN_PAYLOAD}\n{new_payload}"
    )


def archive_review_body(revision: int, review: str) -> str:
    """Render the append-only review paired with a superseded plan."""
    marker = HISTORY_MARKER.format(revision=revision, kind="review")
    return (
        f"{marker}\n## Review of Previous Plan — Revision {revision}\n\n"
        f"{extract_current_review(review)}"
    )


def archived_new_plan(body: str) -> str:
    """Recover the proposed next plan from an immutable plan-history comment."""
    _before, marker, payload = body.partition(_NEW_PLAN_PAYLOAD)
    return payload.strip() if marker else ""


def archived_old_plan(body: str) -> str:
    """Recover the superseded plan from an immutable plan-history comment."""
    _before, marker, payload = body.partition(_OLD_PLAN_PAYLOAD)
    if not marker:
        return ""
    old_plan, _new_marker, _new_plan = payload.partition(_NEW_PLAN_PAYLOAD)
    return old_plan.strip()


def _owned_comments(comments: Sequence[IssueComment | str]) -> list[IssueComment]:
    return [c for raw in comments if (c := as_issue_comment(raw)).viewer_did_author]


def journal_snapshot(comments: Sequence[IssueComment | str]) -> JournalSnapshot:
    """Reconstruct the current plan/review and ordered immutable history."""
    owned = _owned_comments(comments)
    history: list[HistoryArtifact] = []
    current_plan_body = ""
    current_review_body = ""
    for comment in owned:
        body = comment.body.lstrip()
        match = HISTORY_RE.match(body)
        if match:
            history.append(
                HistoryArtifact(
                    revision=int(match.group("revision")),
                    kind=match.group("kind"),
                    body=body,
                )
            )
        elif is_plan_comment(body):
            current_plan_body = body
        elif is_plan_review_comment(body):
            current_review_body = body

    archived_max = max((artifact.revision for artifact in history), default=0)
    explicit_revision = comment_revision(current_plan_body) if current_plan_body else None
    revision = explicit_revision or max(1, archived_max + 1)
    review_revision = comment_revision(current_review_body) if current_review_body else None
    if current_review_body and review_revision is None and archived_max == 0:
        review_revision = revision
    return JournalSnapshot(
        revision=revision,
        current_plan=extract_current_plan(current_plan_body) if current_plan_body else "",
        current_review=(extract_current_review(current_review_body) if current_review_body else ""),
        current_review_revision=review_revision,
        history=tuple(sorted(history, key=lambda item: (item.revision, item.kind != "plan"))),
    )


def review_state(review: str) -> str:
    """Return the final plan-state token without legacy free-text fallback."""
    return parse_plan_review_state(review) or "unparseable"


def _review_reason(review: str) -> str:
    meaningful = [
        line.strip()
        for line in review.splitlines()
        if line.strip() and not line.lstrip().startswith(("<!--", "##", "state:plan-", "Verdict:"))
    ]
    text = " ".join(meaningful)
    if len(text) <= MAX_REVIEW_SUMMARY_CHARS:
        return text
    return f"{text[:MAX_REVIEW_SUMMARY_CHARS].rstrip()}…"


def _history_index(snapshot: JournalSnapshot) -> str:
    """Render a compact index for superseded artifacts and the current revision."""
    lines = ["## Ordered revision index"]
    by_revision: dict[int, dict[str, HistoryArtifact]] = {}
    for artifact in snapshot.history:
        by_revision.setdefault(artifact.revision, {})[artifact.kind] = artifact
    for revision in sorted(by_revision):
        pair = by_revision[revision]
        plan = pair.get("plan")
        review = pair.get("review")
        new_plan = archived_new_plan(plan.body) if plan else ""
        review_payload = extract_current_review(review.body) if review else ""
        next_fingerprint = plan_fingerprint(new_plan) if new_plan else "missing"
        review_reason = _review_reason(review_payload) or "none"
        lines.append(
            f"- Revision {revision}: plan_sha={next_fingerprint}; "
            f"review={review_state(review_payload)}; reason={review_reason}"
        )
    if snapshot.current_plan:
        current_review = (
            snapshot.current_review
            if snapshot.current_review_revision is not None
            and snapshot.current_review_revision >= snapshot.revision
            else ""
        )
        current_reason = _review_reason(current_review) or "none"
        lines.append(
            f"- Revision {snapshot.revision} (current): "
            f"plan_sha={plan_fingerprint(snapshot.current_plan)}; "
            f"review={review_state(current_review)}; reason={current_reason}"
        )
    return "\n".join(lines)


def _bounded_excerpt(text: str, max_chars: int) -> str:
    """Return a deterministic head/tail excerpt that fits *max_chars*."""
    if max_chars <= 0:
        return ""
    if len(text) <= max_chars:
        return text
    marker = "\n\n_[artifact excerpt truncated]_\n\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    remaining = max_chars - len(marker)
    head = (remaining + 1) // 2
    tail = remaining - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


def history_projection(
    comments: Sequence[IssueComment | str], *, max_chars: int = MAX_AGENT_HISTORY_CHARS
) -> str:
    """Return chronological agent context while keeping the complete GitHub journal intact."""
    snapshot = journal_snapshot(comments)
    full_parts = [artifact.body for artifact in snapshot.history]
    if snapshot.current_plan:
        full_parts.append(render_current_plan(snapshot.current_plan, revision=snapshot.revision))
    if (
        snapshot.current_review
        and snapshot.current_review_revision is not None
        and snapshot.current_review_revision >= snapshot.revision
    ):
        full_parts.append(
            render_current_review(snapshot.current_review, revision=snapshot.revision)
        )
    full = "\n\n---\n\n".join(full_parts)
    if len(full) <= max_chars:
        return full

    current_parts: list[tuple[str, str]] = []
    if snapshot.current_plan:
        current_parts.append(
            (
                "## Current plan excerpt",
                render_current_plan(snapshot.current_plan, revision=snapshot.revision),
            )
        )
    if (
        snapshot.current_review
        and snapshot.current_review_revision is not None
        and snapshot.current_review_revision >= snapshot.revision
    ):
        current_parts.append(
            (
                "## Current review excerpt",
                render_current_review(snapshot.current_review, revision=snapshot.revision),
            )
        )
    index = _history_index(snapshot)
    prefix = f"{_TRUNCATION_NOTICE}\n\n{index}"
    separator = "\n\n---\n\n"
    headings_size = sum(len(separator) + len(heading) + 2 for heading, _ in current_parts)
    available = max_chars - len(prefix) - headings_size
    if available < 0:
        raise ValueError("history metadata exceeds the configured agent-history budget")

    rendered = prefix
    remaining = available
    for index_in_parts, (heading, artifact) in enumerate(current_parts):
        parts_left = len(current_parts) - index_in_parts
        excerpt_budget = remaining // parts_left
        excerpt = _bounded_excerpt(artifact, excerpt_budget)
        rendered += f"{separator}{heading}\n\n{excerpt}"
        remaining -= len(excerpt)
    return rendered


def trusted_feedback_after_block(
    comments: Sequence[IssueComment | str],
) -> tuple[IssueComment, ...]:
    """Return trusted maintainer feedback after the newest BLOCKED canonical review."""
    structured = [as_issue_comment(comment) for comment in comments]
    blocked_index: int | None = None
    for index, comment in enumerate(structured):
        if (
            comment.viewer_did_author
            and is_plan_review_comment(comment.body)
            and review_state(extract_current_review(comment.body)) == "state:plan-blocked"
        ):
            blocked_index = index
    if blocked_index is None:
        return ()
    return tuple(
        comment
        for comment in structured[blocked_index + 1 :]
        if comment.is_trusted_human and not is_journal_comment(comment.body)
    )


def feedback_projection(comments: Sequence[IssueComment | str]) -> str:
    """Return bounded chronological journal context plus qualifying human feedback."""
    feedback = trusted_feedback_after_block(comments)
    if not feedback:
        return ""
    feedback_text = "\n\n---\n\n".join(comment.body for comment in feedback)
    truncated = len(feedback_text) > MAX_FEEDBACK_CHARS
    if truncated:
        feedback_text = feedback_text[-MAX_FEEDBACK_CHARS:]
    heading = "## Trusted human feedback (truncated)" if truncated else "## Trusted human feedback"
    suffix = f"{heading}\n\n{feedback_text}"
    separator = "\n\n---\n\n"
    base_budget = max(0, MAX_AGENT_HISTORY_CHARS - len(separator) - len(suffix))
    base = history_projection(comments, max_chars=base_budget)
    return f"{base}{separator if base else ''}{suffix}"
