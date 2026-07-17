"""Versioned, authenticated strict-review lease and verdict grammars.

Strict review uses an immutable GitHub-comment lease per ``(PR, head SHA)``.
The lease is a fencing token: only its elected holder may publish the final
verdict for that head.  Verdict comments are append-only; a later worker can
therefore never PATCH a NOGO into a GO (or vice versa) by winning a
last-writer-wins race.

The original v1 verdict grammar remains parseable for audit/backwards
compatibility, but pipeline merge authorization accepts only the fenced v2
format.  Keeping that policy in the GitHub adapter lets offline tooling still
inspect historical v1 comments without making them merge authority.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Historical, unfenced artifact marker.  Do not use this for new pipeline
#: publications or merge authorization.
STRICT_REVIEW_ARTIFACT_MARKER = "<!-- hephaestus-strict-review: v1 -->"

#: Current immutable verdict marker.  The lease reference is digest-bound.
STRICT_REVIEW_ARTIFACT_V2_MARKER = "<!-- hephaestus-strict-review: v2 -->"

#: Immutable claim marker.  The GitHub comment id elects one claimant.
STRICT_REVIEW_LEASE_MARKER = "<!-- hephaestus-strict-review-lease: v1 -->"

#: Hard cap on the total comment body size.
MAX_ARTIFACT_BYTES = 20_000

_LEASE_ID_RE = r"[A-Za-z0-9_-]{1,128}"
_V1_HEADER_RE = re.compile(
    r"\AHead-SHA: ([0-9a-fA-F]{40})\n"
    r"Digest: ([0-9a-fA-F]{64})\n"
    r"Verdict: (GO|NOGO)\n",
)
_V2_HEADER_RE = re.compile(
    rf"\AHead-SHA: ([0-9a-fA-F]{{40}})\n"
    rf"Lease-ID: ({_LEASE_ID_RE})\n"
    r"Lease-Comment-ID: ([1-9][0-9]*)\n"
    r"Digest: ([0-9a-fA-F]{64})\n"
    r"Verdict: (GO|NOGO)\n",
)
_LEASE_HEADER_RE = re.compile(
    rf"\AHead-SHA: ([0-9a-fA-F]{{40}})\n"
    rf"Lease-ID: ({_LEASE_ID_RE})\n"
    r"Expires-At: ([1-9][0-9]*)\n"
    r"Digest: ([0-9a-fA-F]{64})\n\Z",
)
_FINAL_VERDICT_RE = re.compile(r"(?m)^Grade: ([A-F][+-]?)\nVerdict: (GO|NOGO)[ \t]*\n?\Z")


def _final_verdict_token(verdict_body: str) -> str | None:
    """Return the exact terminal machine verdict from a strict-review body."""
    match = _FINAL_VERDICT_RE.search(verdict_body)
    return match.group(2) if match is not None else None


def _digest(*parts: str) -> str:
    """Return a digest over newline-delimited grammar fields."""
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class ParsedStrictLease:
    """A grammar- and digest-verified immutable review-lease claim."""

    head_sha: str
    lease_id: str
    expires_at: int


@dataclass(frozen=True)
class ParsedStrictArtifact:
    """A grammatically-valid strict verdict (GitHub checks authorship separately)."""

    head_sha: str
    is_go: bool
    verdict: str
    verdict_body: str
    schema_version: int
    lease_id: str | None = None
    lease_comment_id: int | None = None


def render_strict_review_lease(head_sha: str, lease_id: str, *, expires_at: int) -> str:
    """Render an immutable lease claim for one exact PR head."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ValueError(f"head_sha must be a 40-character hex commit SHA, got {head_sha!r}")
    if re.fullmatch(_LEASE_ID_RE, lease_id) is None:
        raise ValueError("lease_id must be 1-128 URL-safe characters")
    if expires_at <= 0:
        raise ValueError("expires_at must be a positive Unix timestamp")
    digest = _digest(head_sha.lower(), lease_id, str(expires_at))
    rendered = (
        f"{STRICT_REVIEW_LEASE_MARKER}\n"
        f"Head-SHA: {head_sha}\n"
        f"Lease-ID: {lease_id}\n"
        f"Expires-At: {expires_at}\n"
        f"Digest: {digest}\n"
    )
    if len(rendered.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("strict-review lease exceeds the byte limit")
    return rendered


def parse_strict_review_lease(body: str) -> ParsedStrictLease | None:
    """Parse one complete, digest-verified immutable lease comment."""
    if len(body.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        return None
    prefix = f"{STRICT_REVIEW_LEASE_MARKER}\n"
    if not body.startswith(prefix):
        return None
    match = _LEASE_HEADER_RE.match(body[len(prefix) :])
    if match is None:
        return None
    head_sha, lease_id, expires_at_text, digest = match.groups()
    if _digest(head_sha.lower(), lease_id, expires_at_text).lower() != digest.lower():
        return None
    return ParsedStrictLease(
        head_sha=head_sha.lower(), lease_id=lease_id, expires_at=int(expires_at_text)
    )


def _render_artifact(
    marker: str,
    head_sha: str,
    verdict_body: str,
    *,
    is_go: bool,
    lease_id: str | None = None,
    lease_comment_id: int | None = None,
) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ValueError(f"head_sha must be a 40-character hex commit SHA, got {head_sha!r}")
    verdict_token = "GO" if is_go else "NOGO"
    if _final_verdict_token(verdict_body) != verdict_token:
        raise ValueError(
            "strict-review artifact body must end in the matching Grade/Verdict contract"
        )
    if lease_id is None:
        digest = _digest(head_sha.lower(), verdict_token, verdict_body)
        rendered = (
            f"{marker}\n"
            f"Head-SHA: {head_sha}\n"
            f"Digest: {digest}\n"
            f"Verdict: {verdict_token}\n\n{verdict_body}"
        )
    else:
        if (
            re.fullmatch(_LEASE_ID_RE, lease_id) is None
            or lease_comment_id is None
            or lease_comment_id < 1
        ):
            raise ValueError("v2 strict-review artifacts require a valid lease id and comment id")
        digest = _digest(
            head_sha.lower(), lease_id, str(lease_comment_id), verdict_token, verdict_body
        )
        rendered = (
            f"{marker}\n"
            f"Head-SHA: {head_sha}\n"
            f"Lease-ID: {lease_id}\n"
            f"Lease-Comment-ID: {lease_comment_id}\n"
            f"Digest: {digest}\n"
            f"Verdict: {verdict_token}\n\n{verdict_body}"
        )
    if len(rendered.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        raise ValueError("strict-review artifact exceeds the byte limit")
    return rendered


def render_strict_review_artifact(head_sha: str, verdict_body: str, *, is_go: bool) -> str:
    """Render the historical v1 artifact for offline compatibility only."""
    return _render_artifact(STRICT_REVIEW_ARTIFACT_MARKER, head_sha, verdict_body, is_go=is_go)


def render_fenced_strict_review_artifact(
    head_sha: str,
    verdict_body: str,
    *,
    is_go: bool,
    lease_id: str,
    lease_comment_id: int,
) -> str:
    """Render an append-only v2 verdict bound to the elected lease fence."""
    return _render_artifact(
        STRICT_REVIEW_ARTIFACT_V2_MARKER,
        head_sha,
        verdict_body,
        is_go=is_go,
        lease_id=lease_id,
        lease_comment_id=lease_comment_id,
    )


def _parse_v1(rest: str) -> ParsedStrictArtifact | None:
    match = _V1_HEADER_RE.match(rest)
    if match is None:
        return None
    head_sha, digest, verdict_token = match.groups()
    verdict_body = rest[match.end() :]
    if verdict_body.startswith("\n"):
        verdict_body = verdict_body[1:]
    if _digest(head_sha.lower(), verdict_token, verdict_body).lower() != digest.lower():
        return None
    if _final_verdict_token(verdict_body) != verdict_token:
        return None
    return ParsedStrictArtifact(
        head_sha=head_sha.lower(),
        is_go=(verdict_token == "GO"),  # noqa: S105 - verdict grammar, not a secret
        verdict=verdict_token,
        verdict_body=verdict_body,
        schema_version=1,
    )


def _parse_v2(rest: str) -> ParsedStrictArtifact | None:
    match = _V2_HEADER_RE.match(rest)
    if match is None:
        return None
    head_sha, lease_id, lease_comment_id_text, digest, verdict_token = match.groups()
    verdict_body = rest[match.end() :]
    if verdict_body.startswith("\n"):
        verdict_body = verdict_body[1:]
    if (
        _digest(
            head_sha.lower(), lease_id, lease_comment_id_text, verdict_token, verdict_body
        ).lower()
        != digest.lower()
    ):
        return None
    if _final_verdict_token(verdict_body) != verdict_token:
        return None
    return ParsedStrictArtifact(
        head_sha=head_sha.lower(),
        is_go=(verdict_token == "GO"),  # noqa: S105 - verdict grammar, not a secret
        verdict=verdict_token,
        verdict_body=verdict_body,
        schema_version=2,
        lease_id=lease_id,
        lease_comment_id=int(lease_comment_id_text),
    )


def parse_strict_review_artifact(body: str) -> ParsedStrictArtifact | None:
    """Parse a v1 or fenced v2 artifact; callers choose their auth policy."""
    if len(body.encode("utf-8")) > MAX_ARTIFACT_BYTES:
        return None
    v1_prefix = f"{STRICT_REVIEW_ARTIFACT_MARKER}\n"
    if body.startswith(v1_prefix):
        return _parse_v1(body[len(v1_prefix) :])
    v2_prefix = f"{STRICT_REVIEW_ARTIFACT_V2_MARKER}\n"
    if body.startswith(v2_prefix):
        return _parse_v2(body[len(v2_prefix) :])
    return None


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "STRICT_REVIEW_ARTIFACT_MARKER",
    "STRICT_REVIEW_ARTIFACT_V2_MARKER",
    "STRICT_REVIEW_LEASE_MARKER",
    "ParsedStrictArtifact",
    "ParsedStrictLease",
    "parse_strict_review_artifact",
    "parse_strict_review_lease",
    "render_fenced_strict_review_artifact",
    "render_strict_review_artifact",
    "render_strict_review_lease",
]
