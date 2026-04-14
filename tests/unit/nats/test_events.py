"""Tests for hephaestus.nats.events."""

from __future__ import annotations

import pytest

from hephaestus.nats.events import NATSEvent, SubjectParts, parse_subject


class TestNATSEvent:
    """Tests for NATSEvent model."""

    def test_creates_event(self) -> None:
        event = NATSEvent(subject="hi.tasks.team.123.created", data={}, timestamp="", sequence=0)
        assert event.subject == "hi.tasks.team.123.created"
        assert event.sequence == 0

    def test_sequence_non_negative(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            NATSEvent(subject="s", data={}, timestamp="", sequence=-1)

    def test_data_dict(self) -> None:
        event = NATSEvent(
            subject="s", data={"key": "value"}, timestamp="2024-01-01T00:00:00Z", sequence=5
        )
        assert event.data["key"] == "value"


class TestParseSubject:
    """Tests for parse_subject()."""

    def test_default_prefix(self) -> None:
        parts = parse_subject("hi.tasks.scylla.abc123.created")
        assert isinstance(parts, SubjectParts)
        assert parts.team == "scylla"
        assert parts.task_id == "abc123"
        assert parts.verb == "created"

    def test_custom_prefix(self) -> None:
        parts = parse_subject("my.prefix.teamA.job42.done", prefix="my.prefix")
        assert parts.team == "teamA"
        assert parts.task_id == "job42"
        assert parts.verb == "done"

    def test_wrong_part_count_raises(self) -> None:
        with pytest.raises(ValueError, match="Expected subject"):
            parse_subject("hi.tasks.only.two")

    def test_too_many_parts_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_subject("hi.tasks.team.id.verb.extra")

    def test_single_prefix_segment(self) -> None:
        parts = parse_subject("tasks.teamX.idY.updated", prefix="tasks")
        assert parts.team == "teamX"
        assert parts.task_id == "idY"
        assert parts.verb == "updated"
