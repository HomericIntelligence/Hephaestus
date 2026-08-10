"""Pure resolution of explicit, exact-head GitHub merge authorization."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

MERGE_AUTHORIZATION_MARKER: Final = "<!-- hephaestus-merge-authorization:v1 -->"

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")
_VALID_STATES = frozenset({"APPROVED", "DISMISSED", "COMMENTED", "PENDING", "CHANGES_REQUESTED"})
_VALID_PERMISSIONS = frozenset({"NONE", "READ", "TRIAGE", "WRITE", "MAINTAIN", "ADMIN"})


class MergeAuthorizationStatus(StrEnum):
    """Deterministic outcomes of merge-authorization resolution."""

    AUTHORIZED = "authorized"
    ABSENT = "absent"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    REPLAYED = "replayed"
    REVOKED = "revoked"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class MergeAuthorization:
    """Immutable capability for one trusted review on one exact PR head."""

    repository: str
    pr_number: int
    head_sha: str
    review_id: str
    review_database_id: int | None
    author_login: str
    author_permission: str
    submitted_at: str
    updated_at: str
    includes_created_edit: bool
    last_edited_at: str | None
    body_digest: str

    def __post_init__(self) -> None:  # noqa: C901
        """Reject values that could not safely represent a merge capability."""
        if not isinstance(self.repository, str) or not self.repository.strip():
            raise ValueError("repository must be a non-empty string")
        if (
            isinstance(self.pr_number, bool)
            or not isinstance(self.pr_number, int)
            or self.pr_number <= 0
        ):
            raise ValueError("pr_number must be a positive integer")
        if not isinstance(self.head_sha, str) or _FULL_SHA_RE.fullmatch(self.head_sha) is None:
            raise ValueError("head_sha must be a full lowercase commit SHA")
        if not isinstance(self.review_id, str) or not self.review_id:
            raise ValueError("review_id must be a non-empty string")
        if self.review_database_id is not None and (
            isinstance(self.review_database_id, bool)
            or not isinstance(self.review_database_id, int)
            or self.review_database_id <= 0
        ):
            raise ValueError("review_database_id must be a positive integer or None")
        if not isinstance(self.author_login, str) or not self.author_login:
            raise ValueError("author_login must be a non-empty string")
        if self.author_permission not in {"WRITE", "ADMIN"}:
            raise ValueError("author_permission must be WRITE or ADMIN")
        for field_name, value in (
            ("submitted_at", self.submitted_at),
            ("updated_at", self.updated_at),
        ):
            if not isinstance(value, str) or not value:
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.includes_created_edit, bool):
            raise ValueError("includes_created_edit must be a boolean")
        if self.last_edited_at is not None and (
            not isinstance(self.last_edited_at, str) or not self.last_edited_at
        ):
            raise ValueError("last_edited_at must be a non-empty string or None")
        if not isinstance(self.body_digest, str) or _DIGEST_RE.fullmatch(self.body_digest) is None:
            raise ValueError("body_digest must be a lowercase SHA-256 digest")


@dataclass(frozen=True)
class MergeAuthorizationResolution:
    """Status and optional immutable capability returned by the resolver."""

    status: MergeAuthorizationStatus
    authorization: MergeAuthorization | None = None
    detail: str = ""


def canonical_body_digest(body: str) -> str:
    """Return the SHA-256 digest used to identify the canonical review body."""
    if not isinstance(body, str):
        raise ValueError("review body must be a string")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def normalize_review_database_id(value: object) -> int | None:
    """Normalize GitHub's nullable BigInt representation.

    GitHub may expose a GraphQL ``BigInt`` as an integer or a canonical
    decimal string. ``None`` is valid because ``fullDatabaseId`` is nullable;
    booleans, zero, negatives, floats, and noncanonical strings are not.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("review database id must be a positive integer or None")
    if isinstance(value, int):
        if value <= 0:
            raise ValueError("review database id must be positive")
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]*", value):
        return int(value)
    raise ValueError("review database id must be a canonical positive decimal")


def _mapping_field(review: Mapping[str, object], field: str) -> object:
    """Return a required review field or raise an unclassifiable-data error."""
    if field not in review:
        raise ValueError(f"review field {field!r} is unavailable")
    return review[field]


def _provenance(review: Mapping[str, object]) -> tuple[str, str, bool, str]:
    """Read the fields needed to establish actor and head provenance."""
    author = _mapping_field(review, "author")
    if not isinstance(author, Mapping):
        raise ValueError("review author is unavailable")
    login = author.get("login")
    actor_type = author.get("__typename")
    if not isinstance(login, str) or not login:
        raise ValueError("review author login is unavailable")
    if not isinstance(actor_type, str) or not actor_type:
        raise ValueError("review author type is unavailable")

    viewer_did_author = _mapping_field(review, "viewerDidAuthor")
    if not isinstance(viewer_did_author, bool):
        raise ValueError("review viewerDidAuthor is unavailable")

    commit = _mapping_field(review, "commit")
    if not isinstance(commit, Mapping):
        raise ValueError("review commit is unavailable")
    commit_oid = commit.get("oid")
    if not isinstance(commit_oid, str) or not commit_oid:
        raise ValueError("review commit OID is unavailable")
    return login, actor_type, viewer_did_author, commit_oid


def _permission(value: str) -> str:
    """Normalize a collaborator permission to the resolver vocabulary."""
    if not isinstance(value, str):
        raise ValueError("repository permission is unavailable")
    normalized = value.strip().upper()
    if normalized not in _VALID_PERMISSIONS:
        raise ValueError("repository permission is malformed")
    if normalized == "MAINTAIN":
        return "WRITE"
    return normalized


def _trusted_actor(
    login: str,
    actor_type: str,
    viewer_did_author: bool,
    *,
    automation_login: str,
    permission_for_actor: Callable[[str], str],
) -> tuple[bool, str]:
    """Return whether actor provenance is trusted and its normalized permission."""
    if (
        actor_type != "User"
        or viewer_did_author
        or login.casefold() == automation_login.casefold()
        or login.casefold().endswith("[bot]")
    ):
        return False, "NONE"
    actor_permission = _permission(permission_for_actor(login))
    return actor_permission in {"WRITE", "ADMIN"}, actor_permission


def _candidate_metadata(
    review: Mapping[str, object],
    body: str,
    permission: str,
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
) -> MergeAuthorization:
    """Validate trusted current-head metadata and build its capability."""
    review_id = review.get("id")
    if not isinstance(review_id, str) or not review_id:
        raise ValueError("review id is malformed")
    if "fullDatabaseId" in review:
        database_key = "fullDatabaseId"
    elif "databaseId" in review:
        database_key = "databaseId"
    else:
        raise ValueError("review fullDatabaseId is malformed")
    database_id = normalize_review_database_id(review.get(database_key))
    state = review.get("state")
    if not isinstance(state, str) or state.upper() not in _VALID_STATES:
        raise ValueError("review state is malformed")
    submitted_at = review.get("submittedAt")
    updated_at = review.get("updatedAt")
    if not isinstance(submitted_at, str) or not submitted_at:
        raise ValueError("review submittedAt is malformed")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("review updatedAt is malformed")
    includes_created_edit = review.get("includesCreatedEdit")
    if not isinstance(includes_created_edit, bool):
        raise ValueError("review includesCreatedEdit is malformed")
    if "lastEditedAt" not in review:
        raise ValueError("review lastEditedAt is malformed")
    last_edited_at = review.get("lastEditedAt")
    if last_edited_at is not None and (not isinstance(last_edited_at, str) or not last_edited_at):
        raise ValueError("review lastEditedAt is malformed")
    author = review.get("author")
    if not isinstance(author, Mapping):
        raise ValueError("review author is malformed")
    login = author.get("login")
    if not isinstance(login, str) or not login:
        raise ValueError("review author login is malformed")
    return MergeAuthorization(
        repository=repository,
        pr_number=pr_number,
        head_sha=head_sha,
        review_id=review_id,
        review_database_id=database_id,
        author_login=login,
        author_permission=permission,
        submitted_at=submitted_at,
        updated_at=updated_at,
        includes_created_edit=includes_created_edit,
        last_edited_at=last_edited_at,
        body_digest=canonical_body_digest(body),
    )


def resolve_merge_authorization(  # noqa: C901
    reviews: Sequence[Mapping[str, object]],
    *,
    repository: str,
    pr_number: int,
    head_sha: str,
    automation_login: str,
    permission_for_actor: Callable[[str], str],
) -> MergeAuthorizationResolution:
    """Resolve exactly one unedited, trusted, current-head approval.

    The input is intentionally a native-review snapshot rather than a copied
    operator payload. Review identity, author, state, edit metadata, and the
    commit OID all remain bound to GitHub's record before a capability exists.
    """
    if not isinstance(repository, str) or not repository.strip():
        raise ValueError("repository must be a non-empty string")
    if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError("pr_number must be a positive integer")
    if not isinstance(head_sha, str) or _FULL_SHA_RE.fullmatch(head_sha) is None:
        raise ValueError("head_sha must be a full lowercase commit SHA")
    if not isinstance(automation_login, str) or not automation_login:
        raise ValueError("automation_login must be a non-empty string")

    active: list[MergeAuthorization] = []
    replayed = False
    revoked = False
    stale = False
    untrusted = False
    marked_ids: set[str] = set()

    for review in reviews:
        if not isinstance(review, Mapping):
            raise ValueError("review node is unavailable")
        body = review.get("body")
        if body != MERGE_AUTHORIZATION_MARKER:
            continue
        review_id = review.get("id")
        if isinstance(review_id, str) and review_id:
            if review_id in marked_ids:
                replayed = True
                continue
            marked_ids.add(review_id)
        login, actor_type, viewer_did_author, commit_oid = _provenance(review)
        current_head = commit_oid == head_sha
        trusted, permission = _trusted_actor(
            login,
            actor_type,
            viewer_did_author,
            automation_login=automation_login,
            permission_for_actor=permission_for_actor,
        )
        if not trusted:
            if current_head:
                untrusted = True
            continue
        if not current_head:
            stale = True
            continue
        try:
            candidate = _candidate_metadata(
                review,
                body,
                permission,
                repository=repository,
                pr_number=pr_number,
                head_sha=head_sha,
            )
        except ValueError:
            replayed = True
            continue
        if candidate.includes_created_edit or candidate.last_edited_at is not None:
            replayed = True
            continue
        state = str(review["state"]).upper()
        if state == "APPROVED":
            active.append(candidate)
        elif state == "DISMISSED":
            revoked = True

    if replayed:
        return MergeAuthorizationResolution(MergeAuthorizationStatus.REPLAYED)
    if len(active) > 1:
        return MergeAuthorizationResolution(MergeAuthorizationStatus.AMBIGUOUS)
    if active:
        return MergeAuthorizationResolution(
            MergeAuthorizationStatus.AUTHORIZED,
            authorization=active[0],
        )
    if revoked:
        return MergeAuthorizationResolution(MergeAuthorizationStatus.REVOKED)
    if stale:
        return MergeAuthorizationResolution(MergeAuthorizationStatus.STALE)
    if untrusted:
        return MergeAuthorizationResolution(MergeAuthorizationStatus.UNTRUSTED)
    return MergeAuthorizationResolution(MergeAuthorizationStatus.ABSENT)


__all__ = [
    "MERGE_AUTHORIZATION_MARKER",
    "MergeAuthorization",
    "MergeAuthorizationResolution",
    "MergeAuthorizationStatus",
    "canonical_body_digest",
    "normalize_review_database_id",
    "resolve_merge_authorization",
]
