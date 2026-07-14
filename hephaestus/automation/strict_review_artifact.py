"""Strict-review artifact grammar: serialize/parse the versioned GO/NOGO proof.

The artifact is the durable, authenticated record that a strict-review agent
independently judged a specific PR head — the single fact ``merge_wait``
trusts before arming auto-merge (issue #2055). It is deliberately NOT trusted
on label alone (a label can be stale, forged, or attached to a different PR
head): the artifact binds a verdict to an exact commit SHA and is only
accepted when authored by the authenticated automation identity.

Grammar (a single PR/issue comment body):

    <!-- hephaestus-strict-review: v1 -->
    Head-SHA: <40-hex-char-commit-sha>
    Digest: <sha256-hex-of-verdict-body>
    Verdict: GO|NOGO

    <verdict body — the reviewer's full output>

Every field is on its own line, in this exact order, immediately after the
marker line. ``parse_strict_review_artifact`` returns ``None`` on ANY
deviation — malformed grammar, wrong SHA length, digest mismatch, oversized
body, or an unrecognized schema version — so a caller can never partially
trust a corrupted or hand-edited comment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: Schema-versioned marker. A future incompatible grammar bumps the version
#: token; parsers only ever accept the versions they understand.
STRICT_REVIEW_ARTIFACT_MARKER = "<!-- hephaestus-strict-review: v1 -->"

#: Hard cap on the total artifact body size. Guards against a runaway or
#: adversarial comment body being treated as authoritative.
MAX_ARTIFACT_BYTES = 20_000

_HEADER_RE = re.compile(
    r"\AHead-SHA:\s*([0-9a-fA-F]{40})\n"
    r"Digest:\s*([0-9a-fA-F]{64})\n"
    r"Verdict:\s*(GO|NOGO)\n",
)


@dataclass(frozen=True)
class ParsedStrictArtifact:
    """A grammatically-valid, digest-verified artifact (authorship NOT checked here).

    Authorship (automation-identity comment authorship) is a GitHub-level
    fact this module has no access to; callers (``PipelineGitHub``) verify
    the comment author before treating a parse result as trustworthy.
    """

    head_sha: str
    is_go: bool
    verdict_body: str


def _digest(head_sha: str, verdict_token: str, verdict_body: str) -> str:
    """SHA-256 over head_sha+verdict_token+verdict_body.

    Covering all three (not just the body) means tampering with any single
    field invalidates the digest.
    """
    payload = f"{head_sha.lower()}\n{verdict_token}\n{verdict_body}".encode()
    return hashlib.sha256(payload).hexdigest()


def render_strict_review_artifact(head_sha: str, verdict_body: str, *, is_go: bool) -> str:
    """Render the versioned artifact comment body for a verdict.

    Args:
        head_sha: The PR's current head commit SHA (40 hex chars).
        verdict_body: The reviewer's full output text.
        is_go: Whether the verdict is GO (True) or NOGO (False).

    Returns:
        The full comment body, marker-prefixed, ready to publish.

    Raises:
        ValueError: If ``head_sha`` is not a 40-character hex string.

    """
    if not re.fullmatch(r"[0-9a-fA-F]{40}", head_sha):
        raise ValueError(f"head_sha must be a 40-character hex commit SHA, got {head_sha!r}")
    verdict_token = "GO" if is_go else "NOGO"
    digest = _digest(head_sha, verdict_token, verdict_body)
    return (
        f"{STRICT_REVIEW_ARTIFACT_MARKER}\n"
        f"Head-SHA: {head_sha}\n"
        f"Digest: {digest}\n"
        f"Verdict: {verdict_token}\n"
        f"\n{verdict_body}"
    )


def parse_strict_review_artifact(body: str) -> ParsedStrictArtifact | None:
    """Parse and digest-verify an artifact comment body.

    Returns ``None`` on ANY of: missing/wrong marker, oversized body,
    malformed header grammar, or a digest that does not match the trailing
    verdict body — a partial or tampered artifact is never partially
    trusted.

    Args:
        body: The full raw comment body.

    Returns:
        A :class:`ParsedStrictArtifact` on success, else ``None``.

    """
    if len(body) > MAX_ARTIFACT_BYTES:
        return None
    if not body.startswith(STRICT_REVIEW_ARTIFACT_MARKER):
        return None
    rest = body[len(STRICT_REVIEW_ARTIFACT_MARKER) :]
    if rest.startswith("\n"):
        rest = rest[1:]
    match = _HEADER_RE.match(rest)
    if match is None:
        return None
    head_sha, digest, verdict_token = match.groups()
    verdict_body = rest[match.end() :]
    if verdict_body.startswith("\n"):
        verdict_body = verdict_body[1:]
    actual_digest = _digest(head_sha, verdict_token, verdict_body)
    if actual_digest.lower() != digest.lower():
        return None
    return ParsedStrictArtifact(
        head_sha=head_sha.lower(),
        is_go=(verdict_token == "GO"),  # noqa: S105 -- verdict token, not a credential
        verdict_body=verdict_body,
    )


__all__ = [
    "MAX_ARTIFACT_BYTES",
    "STRICT_REVIEW_ARTIFACT_MARKER",
    "ParsedStrictArtifact",
    "parse_strict_review_artifact",
    "render_strict_review_artifact",
]
