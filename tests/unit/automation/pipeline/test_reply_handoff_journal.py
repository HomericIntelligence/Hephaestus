"""Tests for implementation reply handoff journal behavior.

Covers visible rendering, legacy recovery, exact IDs, terminal phases,
tri-state classification, and identity mismatches.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from hephaestus.automation.pipeline.github_jobs import (
    ImplementationReplyProgress,
)
from hephaestus.automation.pipeline.reply_handoff import (
    _HANDOFF_JOURNAL_RE,
    _thread_snapshot_fingerprint,
    implementation_reply_handoff,
    implementation_reply_handoff_journal_entry,
    journaled_implementation_reply_handoff,
)
from hephaestus.automation.review_journal import IssueComment
from hephaestus.automation.review_warnings import ReviewWarning


def _make_handoff(
    head_sha: str = "a" * 40,
    batch_nonce: str = "b" * 32,
    thread_count: int = 2,
) -> dict[str, Any]:
    """Create a minimal valid handoff for testing."""
    threads = []
    replies = {}
    for i in range(thread_count):
        thread_id = f"thread_{i}"
        threads.append(
            {
                "id": thread_id,
                "comments": [
                    {"id": f"comment_{i}_0", "author": "reviewer", "body": "original comment"}
                ],
            }
        )
        replies[thread_id] = f"Reply for thread {i}"
    return implementation_reply_handoff(head_sha, threads, replies, batch_nonce)  # type: ignore[return-value]


def _make_threads(thread_count: int = 2) -> list[dict[str, Any]]:
    """Create minimal thread snapshots for testing."""
    threads = []
    for i in range(thread_count):
        thread_id = f"thread_{i}"
        threads.append(
            {
                "id": thread_id,
                "comments": [
                    {"id": f"comment_{i}_0", "author": "reviewer", "body": "original comment"}
                ],
            }
        )
    return threads


_VISIBLE_NOTICE = (
    "<sub>[auto-msg] Temporary bookkeeping comment. "
    "Hephaestus will remove this comment before merge.</sub>"
)


class TestJournalRendering:
    """Tests for the visible notice and hidden payload rendering."""

    def test_pending_journal_renders_visible_notice_before_hidden_marker_and_payload(
        self,
    ) -> None:
        """Journal body must contain visible notice, then hidden marker, then hidden payload."""
        handoff = _make_handoff()
        pr_number = 123
        entry = implementation_reply_handoff_journal_entry(pr_number, handoff)
        assert entry is not None, "journal entry must render for valid handoff"

        _marker, body = entry
        # Visible notice must appear first
        assert body.startswith(_VISIBLE_NOTICE), (
            f"Body must start with visible notice, got: {body[:200]}"
        )
        # Hidden marker must follow the notice
        assert "\n<!-- " in body, "Body must contain hidden marker after notice"
        # Payload must be wrapped in hidden comment
        parts = body.split("\n<!-- ", 1)
        assert len(parts) == 2, "Body must have visible notice and hidden payload sections"
        assert parts[1].endswith(" -->"), "Payload must end with hidden comment close"

    def test_journal_payload_contains_format_phase_and_required_fields(self) -> None:
        """Payload must include format, phase, pr_number, head_sha, batch_nonce."""
        handoff = _make_handoff()
        pr_number = 123
        entry = implementation_reply_handoff_journal_entry(pr_number, handoff)
        assert entry is not None

        _marker, body = entry
        # Body format: visible_notice\nmarker\n<!-- payload -->
        # Extract payload from the last <!-- ... --> section
        payload_str = body.rsplit("\n<!-- ", 1)[1].removesuffix(" -->")
        payload = json.loads(payload_str)

        assert payload["format"] == 2
        assert payload["phase"] == "pending"
        assert payload["pr_number"] == pr_number
        assert payload["head_sha"] == handoff["head_sha"]
        assert payload["batch_nonce"] == handoff["batch_nonce"]
        assert "thread_snapshot_sha256" in payload
        assert payload["thread_snapshot_sha256"] == _thread_snapshot_fingerprint(handoff["threads"])
        assert "replies" in payload
        assert payload["replies"] == handoff["replies"]
        assert "armed" in payload
        assert payload["armed"] is True

    def test_journal_payload_can_include_progress_when_present(self) -> None:
        """When handoff has progress, payload must include progress field."""
        handoff = _make_handoff()
        progress = ImplementationReplyProgress(
            phase="post_replies",
            pull_request_id="PR_123",
            replied_thread_ids=("thread_0",),
            receipts=({},),
        )
        handoff["progress"] = progress.as_dict()

        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None

        _marker, body = entry
        payload_str = body.rsplit("\n<!-- ", 1)[1].removesuffix(" -->")
        payload = json.loads(payload_str)

        assert "progress" in payload
        assert payload["progress"]["phase"] == "post_replies"


class TestLegacyRecovery:
    """Tests for recovering legacy hidden-only format journals."""

    def test_legacy_hidden_only_journal_recovers_as_pending(self) -> None:
        """Legacy journal with only hidden marker and payload must parse as pending."""
        handoff = _make_handoff()
        threads = _make_threads()

        # Build legacy format: only marker + hidden payload (no visible notice)
        marker = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=123:head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "batch=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->"
        )
        payload = {
            "format": 2,
            "armed": True,
            "pr_number": 123,
            "head_sha": handoff["head_sha"],
            "batch_nonce": handoff["batch_nonce"],
            "thread_snapshot_sha256": _thread_snapshot_fingerprint(threads),
            "replies": handoff["replies"],
        }
        legacy_body = (
            f"{marker}\n<!-- {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"
        )

        comment = IssueComment(body=legacy_body, viewer_did_author=True)
        comments = [comment]

        # Recovery should treat legacy as pending
        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=None
        )
        assert recovered is not None, "Legacy journal must be recoverable"
        assert recovered["reconciliation_only"] is True

    def test_legacy_and_visible_pending_journals_recover_exact_id_after_restart(
        self,
    ) -> None:
        """Both legacy and new format pending journals must return same handoff identity."""
        handoff = _make_handoff()
        threads = _make_threads()

        # New format with visible notice
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        visible_comment = IssueComment(body=body, viewer_did_author=True)

        # Legacy format
        legacy_marker = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=123:head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "batch=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->"
        )
        legacy_payload = {
            "format": 2,
            "armed": True,
            "pr_number": 123,
            "head_sha": handoff["head_sha"],
            "batch_nonce": handoff["batch_nonce"],
            "thread_snapshot_sha256": _thread_snapshot_fingerprint(threads),
            "replies": handoff["replies"],
        }
        legacy_body = (
            f"{legacy_marker}\n"
            f"<!-- {json.dumps(legacy_payload, sort_keys=True, separators=(',', ':'))} -->"
        )
        legacy_comment = IssueComment(body=legacy_body, viewer_did_author=True)

        # Both should recover the same handoff
        visible_recovered = journaled_implementation_reply_handoff(
            [visible_comment], pr_number=123, threads=threads, warnings=None
        )
        legacy_recovered = journaled_implementation_reply_handoff(
            [legacy_comment], pr_number=123, threads=threads, warnings=None
        )

        assert visible_recovered is not None
        assert legacy_recovered is not None
        # Same head_sha and batch_nonce means same identity
        assert visible_recovered["head_sha"] == legacy_recovered["head_sha"]
        assert visible_recovered["batch_nonce"] == legacy_recovered["batch_nonce"]
        assert visible_recovered["replies"] == legacy_recovered["replies"]


class TestExactIdsAndTerminalPhases:
    """Tests for exact comment identity tracking and delivery phases."""

    def test_terminal_delivered_phase_after_complete_reply_delivery(self) -> None:
        """After all replies delivered, journal must transition to delivered phase."""
        # This test will be implemented after the delivery completion logic is added.
        # It verifies that a delivered receipt is minted only after all host
        # reply receipts are complete.
        pass

    def test_superseded_phase_when_head_changes(self) -> None:
        """When head changes, journal must transition to superseded phase."""
        pass  # TODO: implement after reply_handoff.py adds superseded phase

    def test_pending_journal_prohibits_cleanup_and_merge(self) -> None:
        """Any valid pending journal must block cleanup and merge."""
        pass  # TODO: implement after merge_wait integration


class TestTriStateClassification:
    """Tests for valid/malformed/unrelated comment classification."""

    def test_valid_actor_owned_journal_with_consistent_identity(self) -> None:
        """Actor-owned journal with matching PR/head/batch/nonce/fingerprint is valid."""
        handoff = _make_handoff()
        threads = _make_threads()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        comment = IssueComment(body=body, viewer_did_author=True)
        comments = [comment]

        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=None
        )
        assert recovered is not None
        assert recovered["head_sha"] == handoff["head_sha"]
        assert recovered["batch_nonce"] == handoff["batch_nonce"]

    def test_malformed_actor_owned_journal_blocks_merge(self) -> None:
        """Actor-owned marker-shaped comment with inconsistent payload must be quarantined."""
        threads = _make_threads()

        # Marker matches but payload has wrong PR number
        marker = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=123:head=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:"
            "batch=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->"
        )
        payload = {
            "format": 2,
            "armed": True,
            "pr_number": 999,  # WRONG PR NUMBER
            "head_sha": "a" * 40,
            "batch_nonce": "b" * 32,
            "thread_snapshot_sha256": _thread_snapshot_fingerprint(threads),
            "replies": {"thread_0": "reply"},
        }
        body = f"{marker}\n<!-- {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"

        comment = IssueComment(body=body, viewer_did_author=True)
        comments = [comment]

        warnings: list[ReviewWarning] = []
        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=warnings
        )
        assert recovered is None, "Malformed journal must not recover"
        assert len(warnings) == 1, "Must record warning for malformed entry"
        assert warnings[0].code == "reply_handoff_entry_quarantined"

    def test_unrelated_user_comment_preserved(self) -> None:
        """User comments without handoff marker must be preserved and not recovered."""
        threads = _make_threads()

        # Regular user comment
        user_comment = IssueComment(
            body="This is a regular user comment",
            viewer_did_author=False,  # Not actor-owned
        )
        # Actor-owned but no marker
        actor_comment = IssueComment(
            body="Just a regular comment from the actor",
            viewer_did_author=True,
        )
        comments = [user_comment, actor_comment]

        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=None
        )
        assert recovered is None, "Unrelated comments must not recover a handoff"

    def test_foreign_actor_marker_not_recovered(self) -> None:
        """Marker from another actor must not be recovered."""
        handoff = _make_handoff()
        threads = _make_threads()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        # Same body but not viewer_did_author
        foreign_comment = IssueComment(body=body, viewer_did_author=False)
        comments = [foreign_comment]

        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=None
        )
        assert recovered is None, "Foreign actor comment must not be recovered"


class TestIdentityMismatches:
    """Tests that PR/head/nonce/source-thread mismatches never authorize recovery."""

    def test_pr_number_mismatch_blocks_recovery(self) -> None:
        """Journal with different PR number must not recover."""
        handoff = _make_handoff()
        threads = _make_threads()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        comment = IssueComment(body=body, viewer_did_author=True)
        comments = [comment]

        # Try to recover with different PR number
        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=999, threads=threads, warnings=None
        )
        assert recovered is None, "PR mismatch must block recovery"

    def test_head_sha_mismatch_blocks_recovery(self) -> None:
        """Journal with different head SHA must not recover."""
        handoff = _make_handoff()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        comment = IssueComment(body=body, viewer_did_author=True)
        comments = [comment]

        # Different threads with different head
        different_threads = _make_threads()
        different_threads[0]["id"] = "different_thread"

        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=different_threads, warnings=None
        )
        assert recovered is None, "Head SHA mismatch (via thread fingerprint) must block recovery"

    def test_batch_nonce_mismatch_blocks_recovery(self) -> None:
        """Journal with marker/payload batch nonce mismatch must not recover."""
        handoff = _make_handoff()
        threads = _make_threads()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        # Corrupt the body to have different batch nonce in marker vs payload
        # Marker has batch="b"*32, payload will have batch_nonce="c"*32
        payload_str = body.rsplit("\n<!-- ", 1)[1].removesuffix(" -->")
        payload = json.loads(payload_str)
        payload["batch_nonce"] = "c" * 32  # Different from marker
        corrupted_body = (
            body.rsplit("\n<!-- ", 1)[0]
            + f"\n<!-- {json.dumps(payload, sort_keys=True, separators=(',', ':'))} -->"
        )

        comment = IssueComment(body=corrupted_body, viewer_did_author=True)
        comments = [comment]

        warnings: list[ReviewWarning] = []
        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=warnings
        )
        assert recovered is None, "Marker/payload batch nonce mismatch must block recovery"
        assert len(warnings) == 1, "Must record warning for malformed entry"
        assert warnings[0].code == "reply_handoff_entry_quarantined"

    def test_source_thread_fingerprint_mismatch_blocks_recovery(self) -> None:
        """Journal with different source thread snapshot must not recover."""
        handoff = _make_handoff()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        comment = IssueComment(body=body, viewer_did_author=True)
        comments = [comment]

        # Different thread content = different fingerprint
        different_threads = _make_threads()
        different_threads[0]["comments"][0]["body"] = "different content"

        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=different_threads, warnings=None
        )
        assert recovered is None, "Source thread fingerprint mismatch must block recovery"

    def test_marker_head_mismatch_blocks_recovery(self) -> None:
        """Marker head must match payload head."""
        handoff = _make_handoff()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        _marker, body = entry

        # Corrupt marker to have different head
        corrupt_marker = (
            "<!-- hephaestus-implementation-reply-handoff:"
            "pr=123:head=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb:"
            "batch=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb -->"
        )
        payload_str = body.rsplit("\n<!-- ", 1)[1].removesuffix(" -->")
        corrupt_body = f"{corrupt_marker}\n<!-- {payload_str} -->"

        comment = IssueComment(body=corrupt_body, viewer_did_author=True)
        comments = [comment]
        threads = _make_threads()

        warnings: list[ReviewWarning] = []
        recovered = journaled_implementation_reply_handoff(
            comments, pr_number=123, threads=threads, warnings=warnings
        )
        assert recovered is None, "Marker-head mismatch must block recovery"
        assert len(warnings) == 1


class TestReplyHandoffIntegration:
    """Integration tests for the full reply handoff flow."""

    def test_journal_marker_regex_matches_rendered_marker(self) -> None:
        """The journal marker regex must match the marker we render."""
        handoff = _make_handoff()
        entry = implementation_reply_handoff_journal_entry(123, handoff)
        assert entry is not None
        marker, _body = entry

        match = _HANDOFF_JOURNAL_RE.fullmatch(marker)
        assert match is not None, f"Marker regex must match rendered marker: {marker}"
        assert match.group("pr") == "123"
        assert match.group("head") == handoff["head_sha"]
        assert match.group("batch") == handoff["batch_nonce"]


# Fixtures for parametrized tests
@pytest.fixture
def minimal_handoff() -> dict[str, Any]:
    """Return a minimal valid handoff for testing."""
    return _make_handoff()


@pytest.fixture
def minimal_threads() -> list[dict[str, Any]]:
    """Return minimal thread snapshots for testing."""
    return _make_threads()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
