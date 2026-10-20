"""Tests for shared GitHub issue comment body utilities."""

from __future__ import annotations

import pytest

from hephaestus.automation import issue_comment_body


def test_visible_issue_comment_text_removes_complete_html_comments() -> None:
    """Complete HTML comments are removed from visible text."""
    body = "<!-- hidden -->Visible text"
    assert issue_comment_body.visible_issue_comment_text(body) == "Visible text"


def test_visible_issue_comment_text_multiline_comments() -> None:
    """Multiline HTML comments are removed completely."""
    body = "<!--\n  hidden\n  metadata\n-->Visible content"
    result = issue_comment_body.visible_issue_comment_text(body)
    assert result == "Visible content"


def test_visible_issue_comment_text_unmatched_open_delimiter_raises() -> None:
    """Unmatched <!-- delimiter raises ValueError."""
    with pytest.raises(ValueError, match="unmatched HTML comment delimiter"):
        issue_comment_body.visible_issue_comment_text("<!-- missing close")


def test_visible_issue_comment_text_unmatched_close_delimiter_raises() -> None:
    """Unmatched --> delimiter raises ValueError."""
    with pytest.raises(ValueError, match="unmatched HTML comment delimiter"):
        issue_comment_body.visible_issue_comment_text("text --> extra")


def test_visible_issue_comment_text_preserves_visible_markdown() -> None:
    """Visible Markdown is preserved after comment removal."""
    body = "<!-- meta -->**Bold** [link](http://example.com)"
    result = issue_comment_body.visible_issue_comment_text(body)
    assert result == "**Bold** [link](http://example.com)"


def test_visible_issue_comment_text_rejects_hidden_only() -> None:
    """Hidden-only body has no visible text."""
    with pytest.raises(ValueError, match="no visible text"):
        issue_comment_body.visible_issue_comment_text("<!-- only hidden -->")


def test_visible_issue_comment_text_rejects_whitespace_only() -> None:
    """Whitespace-only visible content is rejected."""
    with pytest.raises(ValueError, match="no visible text"):
        issue_comment_body.visible_issue_comment_text("<!-- hidden -->   \n  ")


def test_encode_decode_canonical_json() -> None:
    """Canonical JSON encoding and decoding round-trips."""
    data = {"format": 3, "armed": True, "pr_number": 1}
    encoded = issue_comment_body.encode_hidden_comment_json(data)
    # Must be canonical (sorted keys, compact)
    assert '"armed":true' in encoded
    assert '"format":3' in encoded
    assert '"pr_number":1' in encoded
    decoded = issue_comment_body.decode_hidden_comment_json(encoded)
    assert decoded == data


def test_encode_escapes_html_chars() -> None:
    """HTML special chars in JSON are escaped."""
    data = {"value": "<>&"}
    encoded = issue_comment_body.encode_hidden_comment_json(data)
    assert "<" not in encoded or encoded.count("<") == 0  # Should be escaped
    assert "\u003c" in encoded or "&lt;" in encoded


def test_decode_rejects_noncanonical() -> None:
    """Noncanonical JSON is rejected."""
    # Different spacing/ordering - decode checks canonical
    noncanonical = '{"pr_number":1,"format":3,"armed":true}'  # different order
    # But our encoder sorts - so noncanonical form won't match
    with pytest.raises(ValueError, match="canonical"):
        issue_comment_body.decode_hidden_comment_json(noncanonical)


def test_decode_rejects_duplicate_keys() -> None:
    """Duplicate JSON keys are rejected."""
    with pytest.raises(ValueError):
        issue_comment_body.decode_hidden_comment_json('{"a":1,"a":2}')
