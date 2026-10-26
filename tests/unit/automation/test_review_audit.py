"""Reading a reviewer's response."""

from __future__ import annotations

import json

from hephaestus.automation.review_audit import is_clean_go_review, parse_review_audit


def _block(payload: object) -> str:
    return f"Notes first.\n\n```json\n{json.dumps(payload)}\n```"


def test_comment_needs_only_a_body() -> None:
    """Comment needs only a body."""
    audit = parse_review_audit(_block({"comments": [{"body": "fix this"}]}))

    assert audit.valid
    assert audit.findings == ({"body": "fix this"},)
    assert not is_clean_go_review(audit)


def test_fields_the_host_does_not_know_are_ignored() -> None:
    """Fields the host does not know are ignored; grade and severity are kept for display."""
    comment = {"path": "a.py", "line": 3, "side": "LEFT", "severity": "Major", "body": "x"}

    audit = parse_review_audit(_block({"grade": "b", "comments": [comment], "extra": 1}))

    assert audit.findings == ({"body": "x", "path": "a.py", "line": 3, "severity": "major"},)
    assert audit.grade == "B"


def test_an_unusable_severity_is_dropped_not_rejected() -> None:
    """A severity that is not one short word does not fail the review."""
    comment = {"severity": "<!-- x -->", "body": "x"}

    audit = parse_review_audit(_block({"comments": [comment]}))

    assert audit.valid
    assert audit.findings == ({"body": "x"},)


def test_a_comment_that_is_not_an_object_is_kept_as_text() -> None:
    """A comment that is not an object is kept as text."""
    audit = parse_review_audit(_block({"comments": ["rename the helper", {"path": "a.py"}]}))

    assert [f["body"] for f in audit.findings] == ["rename the helper"]


def test_text_without_json_is_valid_feedback() -> None:
    """Text without json is valid feedback."""
    audit = parse_review_audit("Looks fine.")

    assert audit.valid
    assert audit.findings == ()
    assert audit.raw_feedback == "Looks fine."
    assert is_clean_go_review(audit)


def test_a_failed_agent_envelope_is_not_a_review() -> None:
    """A failed agent envelope is not a review."""
    assert not parse_review_audit({"is_error": True, "result": "boom"}).valid
    assert not parse_review_audit("   ").valid


def test_host_control_markers_are_removed_from_comments() -> None:
    """Host control markers are removed from comments."""
    body = "keep\n<!-- hephaestus-severity: major -->\nthis"

    audit = parse_review_audit(_block({"comments": [{"body": body}]}))

    assert "hephaestus-severity" not in str(audit.findings[0]["body"])
    assert "keep" in str(audit.findings[0]["body"])
