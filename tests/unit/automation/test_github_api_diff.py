"""Behavior tests for bounded pull-request review-anchor validation."""

from __future__ import annotations

import pytest

from hephaestus.automation.github_api import _validate_comments_to_diff

DIFF = (
    "diff --git a/mod.py b/mod.py\n"
    "--- a/mod.py\n"
    "+++ b/mod.py\n"
    "@@ -10,2 +10,2 @@\n"
    "-old\n"
    "+new\n"
    " context\n"
)


@pytest.mark.parametrize(
    ("finding", "diff_text", "reason"),
    [
        ({"path": "mod.py", "line": 10, "side": "RIGHT"}, "", "reviewed_diff_unavailable"),
        ({"path": "other.py", "line": 10, "side": "RIGHT"}, DIFF, "path_not_in_diff"),
        ({"path": "mod.py", "line": 99, "side": "RIGHT"}, DIFF, "line_not_in_diff"),
        ({"path": "mod.py", "line": 10, "side": "LEFT"}, DIFF, "unsupported_side"),
    ],
)
def test_anchor_validation_returns_specific_reason(
    finding: dict[str, object], diff_text: str, reason: str
) -> None:
    """Each invalid condition has the correction reason that owns its repair."""
    finding.update(severity="major", body="Preserve this finding.")

    result = _validate_comments_to_diff([finding], diff_text)

    assert result.valid == ()
    assert len(result.corrections) == 1
    assert result.corrections[0].reason == reason


def test_anchor_validation_rejects_more_than_the_finding_limit() -> None:
    """An agent response cannot create unbounded validation work."""
    finding = {
        "path": "mod.py",
        "line": 10,
        "side": "RIGHT",
        "severity": "major",
        "body": "Finding",
    }

    with pytest.raises(ValueError, match="review finding count exceeds"):
        _validate_comments_to_diff([dict(finding) for _ in range(65)], DIFF)


def test_anchor_validation_rejects_oversized_finding_content() -> None:
    """A correction record cannot retain an unbounded finding body."""
    finding = {
        "path": "mod.py",
        "line": 99,
        "side": "RIGHT",
        "severity": "major",
        "body": "x" * 20_000,
    }

    with pytest.raises(ValueError, match="review finding body exceeds"):
        _validate_comments_to_diff([finding], DIFF)


def test_anchor_validation_assigns_stable_ids_to_valid_findings() -> None:
    """A valid finding gets the same host identity as a correction record."""
    finding = {
        "path": "mod.py",
        "line": 10,
        "side": "RIGHT",
        "severity": "major",
        "body": "Finding",
    }

    first = _validate_comments_to_diff([finding], DIFF)
    second = _validate_comments_to_diff([finding], DIFF)

    assert first.valid[0]["finding_id"] == second.valid[0]["finding_id"]


def test_anchor_validation_partitions_a_mixed_finding_batch() -> None:
    """One invalid anchor does not remove a valid finding from the batch."""
    valid = {
        "path": "mod.py",
        "line": 10,
        "side": "RIGHT",
        "severity": "major",
        "body": "Valid finding",
    }
    invalid = {
        "path": "mod.py",
        "line": 99,
        "side": "RIGHT",
        "severity": "major",
        "body": "Invalid anchor",
        "evidence": "Keep this evidence",
    }

    result = _validate_comments_to_diff([valid, invalid], DIFF)

    assert [finding["body"] for finding in result.valid] == ["Valid finding"]
    assert [correction.finding["body"] for correction in result.corrections] == ["Invalid anchor"]
    assert result.corrections[0].finding["evidence"] == "Keep this evidence"


def test_anchor_validation_preserves_an_all_invalid_batch_for_correction() -> None:
    """Every invalid finding receives a typed correction record."""
    findings = [
        {
            "path": "missing.py",
            "line": 1,
            "side": "RIGHT",
            "severity": "major",
            "body": "Missing path",
        },
        {
            "path": "mod.py",
            "line": 99,
            "side": "RIGHT",
            "severity": "minor",
            "body": "Missing line",
        },
    ]

    result = _validate_comments_to_diff(findings, DIFF)

    assert result.valid == ()
    assert [correction.reason for correction in result.corrections] == [
        "path_not_in_diff",
        "line_not_in_diff",
    ]
    assert [correction.finding["body"] for correction in result.corrections] == [
        "Missing path",
        "Missing line",
    ]
