"""Grammar/digest unit tests for the strict-review artifact (issue #2055).

Isolated from GitHub I/O — pure parse/render round-trip and rejection of
malformed/tampered/oversized bodies. Authorship (automation-identity
comment authorship) is verified one layer up, in ``PipelineGitHub``.
"""

from __future__ import annotations

import hashlib

import pytest

from hephaestus.automation.strict_review_artifact import (
    MAX_ARTIFACT_BYTES,
    STRICT_REVIEW_ARTIFACT_MARKER,
    parse_strict_review_artifact,
    render_strict_review_artifact,
)

_SHA = "a" * 40


class TestRenderParseRoundTrip:
    """render_strict_review_artifact / parse_strict_review_artifact round-trip."""

    def test_go_round_trips(self) -> None:
        body = render_strict_review_artifact(_SHA, "Grade: A\nVerdict: GO", is_go=True)
        parsed = parse_strict_review_artifact(body)
        assert parsed is not None
        assert parsed.head_sha == _SHA
        assert parsed.is_go is True
        assert parsed.verdict_body == "Grade: A\nVerdict: GO"

    def test_nogo_round_trips(self) -> None:
        body = render_strict_review_artifact(_SHA, "Grade: D\nVerdict: NOGO", is_go=False)
        parsed = parse_strict_review_artifact(body)
        assert parsed is not None
        assert parsed.is_go is False

    def test_rendered_body_starts_with_marker(self) -> None:
        body = render_strict_review_artifact(_SHA, "text", is_go=True)
        assert body.startswith(STRICT_REVIEW_ARTIFACT_MARKER)

    def test_head_sha_normalized_lowercase(self) -> None:
        body = render_strict_review_artifact("A" * 40, "text", is_go=True)
        parsed = parse_strict_review_artifact(body)
        assert parsed is not None
        assert parsed.head_sha == "a" * 40


class TestRenderValidation:
    """render_strict_review_artifact input validation."""

    def test_rejects_short_sha(self) -> None:
        with pytest.raises(ValueError, match="40-character hex"):
            render_strict_review_artifact("abc123", "text", is_go=True)

    def test_rejects_non_hex_sha(self) -> None:
        with pytest.raises(ValueError, match="40-character hex"):
            render_strict_review_artifact("z" * 40, "text", is_go=True)


class TestParseRejectsMalformed:
    """parse_strict_review_artifact rejects every malformed/tampered shape."""

    def test_rejects_missing_marker(self) -> None:
        assert parse_strict_review_artifact("Head-SHA: " + _SHA) is None

    def test_rejects_wrong_marker_version(self) -> None:
        body = render_strict_review_artifact(_SHA, "text", is_go=True).replace("v1", "v2")
        assert parse_strict_review_artifact(body) is None

    def test_rejects_tampered_digest(self) -> None:
        body = render_strict_review_artifact(_SHA, "original text", is_go=True)
        tampered = body.replace("original text", "swapped text")
        assert parse_strict_review_artifact(tampered) is None

    def test_rejects_tampered_verdict_with_stale_digest(self) -> None:
        """Flipping NOGO->GO without recomputing the digest must fail closed."""
        body = render_strict_review_artifact(_SHA, "text", is_go=False)
        tampered = body.replace("Verdict: NOGO", "Verdict: GO")
        assert parse_strict_review_artifact(tampered) is None

    def test_rejects_malformed_header_order(self) -> None:
        digest = hashlib.sha256(b"text").hexdigest()
        body = (
            f"{STRICT_REVIEW_ARTIFACT_MARKER}\n"
            f"Verdict: GO\n"
            f"Head-SHA: {_SHA}\n"
            f"Digest: {digest}\n\ntext"
        )
        assert parse_strict_review_artifact(body) is None

    def test_rejects_invalid_verdict_token(self) -> None:
        digest = hashlib.sha256(b"text").hexdigest()
        body = (
            f"{STRICT_REVIEW_ARTIFACT_MARKER}\nHead-SHA: {_SHA}\n"
            f"Digest: {digest}\nVerdict: MAYBE\n\ntext"
        )
        assert parse_strict_review_artifact(body) is None

    def test_rejects_oversized_body(self) -> None:
        huge = "x" * (MAX_ARTIFACT_BYTES + 1)
        body = render_strict_review_artifact(_SHA, huge, is_go=True)
        assert parse_strict_review_artifact(body) is None

    def test_rejects_short_sha_in_header(self) -> None:
        digest = hashlib.sha256(b"text").hexdigest()
        body = (
            f"{STRICT_REVIEW_ARTIFACT_MARKER}\nHead-SHA: abc123\n"
            f"Digest: {digest}\nVerdict: GO\n\ntext"
        )
        assert parse_strict_review_artifact(body) is None

    def test_rejects_empty_body(self) -> None:
        assert parse_strict_review_artifact("") is None

    def test_rejects_foreign_comment_body(self) -> None:
        assert parse_strict_review_artifact("Just a regular PR comment, nothing special.") is None
