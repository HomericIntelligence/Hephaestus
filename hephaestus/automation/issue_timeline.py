"""Pure planning helpers for compacting automation-owned issue comments."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hephaestus.automation.protocol import PLAN_CANONICAL_MARKER, PLAN_REVIEW_CANONICAL_MARKER
from hephaestus.automation.review_journal import (
    HISTORY_MARKER_PREFIX,
    HISTORY_RE,
    IssueComment,
    archived_new_plan,
    archived_old_plan,
    comment_revision,
    extract_current_plan,
    has_exact_leading_marker,
    is_plan_comment,
    is_plan_review_comment,
    journal_snapshot,
    plan_fingerprint,
    render_current_plan,
    render_current_review,
)
from hephaestus.automation.state_labels import SKIP_REASON_MARKER

IMPLEMENTATION_REPLY_HANDOFF_MARKER_PREFIX = "<!-- hephaestus-implementation-reply-handoff:"
_IMPLEMENTATION_REPLY_HANDOFF_MARKER_RE = re.compile(
    r"<!-- hephaestus-implementation-reply-handoff:pr=\d+:"
    r"head=[0-9a-f]{40}(?:[0-9a-f]{24})?:batch=[0-9a-f]{32} -->"
)


@dataclass(frozen=True)
class IssueTimelineCompaction:
    """One issue's safe canonical updates and actor-owned deletions."""

    plan_body: str | None = None
    review_body: str | None = None
    plan_needs_update: bool = False
    review_needs_update: bool = False
    delete_comment_ids: tuple[int, ...] = ()

    @property
    def has_changes(self) -> bool:
        """Return whether applying this plan would mutate GitHub."""
        return bool(self.plan_needs_update or self.review_needs_update or self.delete_comment_ids)


def issue_comments_from_metadata(
    metadata: Sequence[dict[str, Any]],
    *,
    viewer_login: str,
) -> list[IssueComment]:
    """Convert GitHub REST metadata into ownership-aware comments."""
    normalized_login = viewer_login.strip().lower()
    comments: list[IssueComment] = []
    for raw in metadata:
        author = raw.get("user") or raw.get("author")
        author_login = str(author.get("login", "")) if isinstance(author, dict) else ""
        if "viewerDidAuthor" in raw:
            owned = raw.get("viewerDidAuthor") is True
        else:
            owned = bool(normalized_login and author_login.lower() == normalized_login)
        raw_id = raw.get("databaseId", raw.get("id"))
        comments.append(
            IssueComment(
                body=str(raw.get("body", "")),
                author_login=author_login,
                author_association=str(
                    raw.get("author_association") or raw.get("authorAssociation") or ""
                ),
                created_at=str(raw.get("created_at") or raw.get("createdAt") or ""),
                updated_at=str(raw.get("updated_at") or raw.get("updatedAt") or ""),
                viewer_did_author=owned,
                database_id=int(raw_id) if raw_id is not None else None,
                url=str(raw.get("html_url") or raw.get("url") or ""),
            )
        )
    return comments


def _validate_legacy_markers(comments: Sequence[IssueComment]) -> None:
    """Reject prefix collisions before planning any destructive mutation."""
    for comment in comments:
        first_line = comment.body.partition("\n")[0]
        if (
            first_line.startswith(HISTORY_MARKER_PREFIX)
            and HISTORY_RE.fullmatch(first_line) is None
        ):
            marker, terminator, _suffix = first_line.partition(" -->")
            if not terminator or HISTORY_RE.fullmatch(f"{marker}{terminator}") is None:
                raise RuntimeError("malformed legacy automation marker; manual review is required")
        if (
            first_line.startswith(IMPLEMENTATION_REPLY_HANDOFF_MARKER_PREFIX)
            and _IMPLEMENTATION_REPLY_HANDOFF_MARKER_RE.fullmatch(first_line) is None
        ):
            raise RuntimeError("malformed legacy automation marker; manual review is required")


def _is_obsolete_automation_comment(body: str) -> bool:
    """Return whether an owned comment belongs to a removable automation role."""
    return bool(
        is_plan_comment(body)
        or is_plan_review_comment(body)
        or HISTORY_RE.match(body) is not None
        or body.startswith(SKIP_REASON_MARKER)
        or body.startswith(IMPLEMENTATION_REPLY_HANDOFF_MARKER_PREFIX)
    )


def _has_exact_leading_marker(body: str, marker: str) -> bool:
    """Return whether *marker* is the exact first raw line of *body*."""
    return has_exact_leading_marker(body, marker)


def _history_has_canonical_pointer(
    comment: IssueComment,
    *,
    owned: Sequence[IssueComment],
    target_plan: IssueComment | None,
    target_review: IssueComment | None,
) -> bool:
    """Return whether a legacy artifact has an exactly reconstructed successor.

    Legacy archives are recovery evidence, not merely redundant copies of an
    arbitrary current pointer.  A plan archive can disappear only after the
    canonical plan at the archive's immediate successor revision contains the
    archived recovery payload.  A review archive needs that same verified plan
    successor and its own immediate canonical review successor.
    """
    history_match = HISTORY_RE.match(comment.body)
    if history_match is None:
        return True
    revision = int(history_match.group("revision"))
    successor_revision = revision + 1
    plan_is_exact_successor = bool(
        target_plan is not None
        and comment_revision(target_plan.body) == successor_revision
        and extract_current_plan(target_plan.body) == archived_new_plan(comment.body)
    )
    if history_match.group("kind") == "plan":
        return plan_is_exact_successor

    # A review archive does not carry a future review payload.  It therefore
    # needs a same-revision plan archive whose recovery payload reconstructs
    # the canonical plan, plus the canonical review for that exact successor.
    matching_plan_archive = any(
        (match := HISTORY_RE.match(candidate.body)) is not None
        and match.group("kind") == "plan"
        and int(match.group("revision")) == revision
        and target_plan is not None
        and comment_revision(target_plan.body) == successor_revision
        and extract_current_plan(target_plan.body) == archived_new_plan(candidate.body)
        for candidate in owned
    )
    return bool(
        matching_plan_archive
        and target_review is not None
        and comment_revision(target_review.body) == successor_revision
    )


def _obsolete_comment_ids(
    owned: Sequence[IssueComment],
    *,
    keep_ids: set[int],
    target_plan: IssueComment | None,
    target_review: IssueComment | None,
) -> list[int]:
    """Return owned obsolete IDs whose canonical replacement is durable."""
    delete_ids: list[int] = []
    for comment in owned:
        if not _is_obsolete_automation_comment(comment.body) or comment.database_id in keep_ids:
            continue
        if not _history_has_canonical_pointer(
            comment,
            owned=owned,
            target_plan=target_plan,
            target_review=target_review,
        ):
            # A legacy archive is the sole recoverable representation of its
            # artifact until a canonical pointer exists. Retain it rather than
            # compacting history into data loss.
            continue
        if comment.database_id is None:
            raise RuntimeError("owned obsolete automation comment has no database id")
        delete_ids.append(comment.database_id)
    return delete_ids


def plan_issue_timeline_compaction(
    comments: Sequence[IssueComment],
) -> IssueTimelineCompaction:
    """Return the safe mutations that enforce two canonical automation comments.

    Only comments GitHub identifies as authored by the authenticated viewer are
    considered. Human and foreign marker-bearing comments are intentionally
    invisible to this planner.
    """
    owned = [comment for comment in comments if comment.viewer_did_author]
    if not owned:
        return IssueTimelineCompaction()

    _validate_legacy_markers(owned)

    # Parsing first makes conflicting or malformed legacy journal identities a
    # per-issue hard failure before a destructive action is planned.
    snapshot = journal_snapshot(owned)
    plan_comments = [comment for comment in owned if is_plan_comment(comment.body)]
    review_comments = [comment for comment in owned if is_plan_review_comment(comment.body)]
    canonical_plans = [
        comment
        for comment in plan_comments
        if _has_exact_leading_marker(comment.body, PLAN_CANONICAL_MARKER)
    ]
    canonical_reviews = [
        comment
        for comment in review_comments
        if _has_exact_leading_marker(comment.body, PLAN_REVIEW_CANONICAL_MARKER)
    ]
    target_plan = (
        canonical_plans[-1] if canonical_plans else (plan_comments[-1] if plan_comments else None)
    )
    target_review = (
        canonical_reviews[-1]
        if canonical_reviews
        else (review_comments[-1] if review_comments else None)
    )

    prior_fingerprints = list(snapshot.prior_plan_fingerprints)
    for comment in plan_comments[:-1]:
        prior_fingerprints.append(plan_fingerprint(comment.body))
    for artifact in snapshot.history:
        if artifact.kind == "plan":
            for plan in (archived_old_plan(artifact.body), archived_new_plan(artifact.body)):
                if plan:
                    prior_fingerprints.append(plan_fingerprint(plan))

    plan_body = (
        render_current_plan(
            snapshot.current_plan,
            revision=snapshot.revision,
            prior_fingerprints=tuple(dict.fromkeys(prior_fingerprints)),
        )
        if target_plan is not None
        else None
    )
    review_body = (
        render_current_review(
            snapshot.current_review,
            revision=snapshot.current_review_revision or snapshot.revision,
        )
        if target_review is not None
        else None
    )

    keep_ids = {
        comment.database_id
        for comment in (target_plan, target_review)
        if comment is not None and comment.database_id is not None
    }
    delete_ids = _obsolete_comment_ids(
        owned,
        keep_ids=keep_ids,
        target_plan=target_plan,
        target_review=target_review,
    )

    # Canonical target bodies are PATCHed in place by the caller. Prefixing is
    # asserted here so a parser regression cannot turn an upsert into a new
    # third comment.
    if plan_body is not None and not plan_body.startswith(PLAN_CANONICAL_MARKER):
        raise AssertionError("canonical plan renderer lost its ownership marker")
    if review_body is not None and not review_body.startswith(PLAN_REVIEW_CANONICAL_MARKER):
        raise AssertionError("canonical review renderer lost its ownership marker")
    return IssueTimelineCompaction(
        plan_body=plan_body,
        review_body=review_body,
        plan_needs_update=bool(target_plan is not None and target_plan.body != plan_body),
        review_needs_update=bool(target_review is not None and target_review.body != review_body),
        delete_comment_ids=tuple(delete_ids),
    )
