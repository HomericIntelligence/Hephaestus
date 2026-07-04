"""Tests for WorkItem, ItemKind, ItemResult, and HistoryEvent."""

from datetime import datetime, timezone

import pytest

from hephaestus.automation.pipeline import (
    HistoryEvent,
    ItemKind,
    ItemResult,
    StageName,
    WorkItem,
)


class TestItemKind:
    """Tests for ItemKind enum."""

    def test_item_kind_values(self) -> None:
        """Verify ItemKind has expected enum values."""
        assert ItemKind.REPO.value == "repo"
        assert ItemKind.ISSUE.value == "issue"
        assert ItemKind.PR.value == "pr"

    def test_item_kind_string_behavior(self) -> None:
        """ItemKind inherits from str."""
        assert isinstance(ItemKind.REPO, str)
        assert ItemKind.REPO == "repo"


class TestHistoryEvent:
    """Tests for HistoryEvent dataclass."""

    def test_history_event_creation(self) -> None:
        """Create a HistoryEvent with required and optional fields."""
        now = datetime.now()
        event = HistoryEvent(
            timestamp=now, stage=StageName.PLANNING, state="in_progress", note="test"
        )
        assert event.timestamp == now
        assert event.stage == StageName.PLANNING
        assert event.state == "in_progress"
        assert event.note == "test"

    def test_history_event_default_note(self) -> None:
        """HistoryEvent.note defaults to empty string."""
        event = HistoryEvent(timestamp=datetime.now(), stage=StageName.REPO, state="queued")
        assert event.note == ""

    def test_history_event_frozen(self) -> None:
        """HistoryEvent is frozen (immutable)."""
        event = HistoryEvent(timestamp=datetime.now(), stage=StageName.REPO, state="queued")
        with pytest.raises(AttributeError):
            event.state = "modified"  # type: ignore


class TestItemResult:
    """Tests for ItemResult dataclass."""

    def test_item_result_creation(self) -> None:
        """Create an ItemResult with all fields."""
        result = ItemResult(passed=True, reason="all stages passed", final_stage=StageName.FINISHED)
        assert result.passed is True
        assert result.reason == "all stages passed"
        assert result.final_stage == StageName.FINISHED

    def test_item_result_frozen(self) -> None:
        """ItemResult is frozen (immutable)."""
        result = ItemResult(passed=True, reason="ok", final_stage=StageName.FINISHED)
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore


class TestWorkItem:
    """Tests for WorkItem dataclass."""

    def test_work_item_creation_minimal(self) -> None:
        """Create a WorkItem with minimal fields."""
        item = WorkItem(repo="myrepo", kind=ItemKind.REPO)
        assert item.repo == "myrepo"
        assert item.kind == ItemKind.REPO
        assert item.stage == StageName.REPO
        assert item.state == ""
        assert len(item.history) == 0
        assert item.result is None

    def test_work_item_issue_pr_optional(self) -> None:
        """Issue and pr fields are optional."""
        item = WorkItem(repo="repo", kind=ItemKind.ISSUE, issue=123, pr=456)
        assert item.issue == 123
        assert item.pr == 456

    def test_work_item_default_attempts(self) -> None:
        """WorkItem initializes all attempt counters to 0."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        expected_keys = {
            "clone",
            "plan",
            "plan_review_iter",
            "plan_cycles",
            "implement",
            "pr_review_iter",
            "pr_review_hard",
            "test_fix",
            "ci_fix",
            "blocked_address",
            "rebase",
            "merge",
        }
        assert set(item.attempts.keys()) == expected_keys
        assert all(v == 0 for v in item.attempts.values())

    def test_work_item_mutable_defaults_do_not_alias(self) -> None:
        """Two WorkItems never share attempts/history/session_ids/payload."""
        a = WorkItem(repo="repo", kind=ItemKind.REPO)
        b = WorkItem(repo="repo", kind=ItemKind.REPO)
        a.attempts["plan"] = 5
        a.session_ids["implementer"] = "sid-1"
        a.payload["k"] = "v"
        a.add_history_event(StageName.PLANNING, "running")

        assert b.attempts["plan"] == 0
        assert b.session_ids == {}
        assert b.payload == {}
        assert len(b.history) == 0

    def test_work_item_timestamps_are_utc_aware(self) -> None:
        """created_at/updated_at and history timestamps are tz-aware UTC."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        assert item.created_at.tzinfo is not None
        assert item.updated_at.tzinfo is not None
        item.add_history_event(StageName.PLANNING, "running")
        assert item.history[0].timestamp.tzinfo is not None
        assert item.updated_at == item.history[0].timestamp

    def test_work_item_history_cap_at_200(self) -> None:
        """History is capped at 200 events; oldest events are dropped."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        for i in range(250):
            item.add_history_event(StageName.PLANNING, "in_progress", note=f"event_{i}")

        assert len(item.history) == 200
        # History[0] should be event 50 (the first 50 were dropped)
        assert item.history[0].note == "event_50"
        assert item.history[-1].note == "event_249"

    def test_work_item_history_exactly_at_cap_drops_nothing(self) -> None:
        """Exactly HISTORY_CAP events: nothing dropped, event_0 still first."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        for i in range(200):
            item.add_history_event(StageName.PLANNING, "in_progress", note=f"event_{i}")

        assert len(item.history) == 200
        assert item.history[0].note == "event_0"
        assert item.history[-1].note == "event_199"

    def test_work_item_add_history_event(self) -> None:
        """add_history_event records stage transitions."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        before = datetime.now(timezone.utc)
        item.add_history_event(StageName.PLANNING, "running", note="started")
        after = datetime.now(timezone.utc)

        assert len(item.history) == 1
        event = item.history[0]
        assert event.stage == StageName.PLANNING
        assert event.state == "running"
        assert event.note == "started"
        assert before <= event.timestamp <= after

    def test_work_item_labels_cache(self) -> None:
        """labels_cache is an empty dict by default."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        assert isinstance(item.labels_cache, dict)
        assert len(item.labels_cache) == 0
        item.labels_cache["state:implementation-go"] = True
        assert item.labels_cache["state:implementation-go"] is True

    def test_work_item_payload_scratch(self) -> None:
        """Payload is an empty dict for scratch data."""
        item = WorkItem(repo="repo", kind=ItemKind.REPO)
        assert isinstance(item.payload, dict)
        item.payload["plan"] = {"objectives": ["test"]}
        assert item.payload["plan"]["objectives"] == ["test"]
