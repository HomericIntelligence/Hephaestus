"""Tests for hephaestus.nats.handlers."""

from __future__ import annotations

from unittest.mock import MagicMock

from hephaestus.nats.events import NATSEvent
from hephaestus.nats.handlers import EventRouter, create_default_router


def _event(subject: str) -> NATSEvent:
    return NATSEvent(subject=subject, data={}, timestamp="", sequence=1)


class TestEventRouter:
    """Tests for EventRouter."""

    def test_register_and_dispatch(self) -> None:
        router = EventRouter()
        mock_handler = MagicMock()
        router.register("created", mock_handler)
        event = _event("hi.tasks.team.123.created")
        router.dispatch(event)
        mock_handler.assert_called_once_with(event)

    def test_no_handler_for_verb(self) -> None:
        router = EventRouter()
        # dispatch without registering — should not raise
        router.dispatch(_event("hi.tasks.team.123.deleted"))

    def test_unparseable_subject_no_raise(self) -> None:
        router = EventRouter()
        router.dispatch(_event("unparseable"))

    def test_handler_exception_caught(self) -> None:
        router = EventRouter()

        def bad_handler(event: NATSEvent) -> None:
            raise RuntimeError("boom")

        router.register("created", bad_handler)
        # Should not raise
        router.dispatch(_event("hi.tasks.team.123.created"))

    def test_overwrite_handler(self) -> None:
        router = EventRouter()
        first = MagicMock()
        second = MagicMock()
        router.register("created", first)
        router.register("created", second)
        router.dispatch(_event("hi.tasks.team.123.created"))
        first.assert_not_called()
        second.assert_called_once()


class TestCreateDefaultRouter:
    """Tests for create_default_router()."""

    def test_returns_event_router(self) -> None:
        router = create_default_router()
        assert isinstance(router, EventRouter)

    def test_has_created_handler(self) -> None:
        router = create_default_router()
        # Should dispatch without error
        router.dispatch(_event("hi.tasks.team.123.created"))

    def test_has_updated_handler(self) -> None:
        router = create_default_router()
        router.dispatch(_event("hi.tasks.team.123.updated"))

    def test_has_completed_handler(self) -> None:
        router = create_default_router()
        router.dispatch(_event("hi.tasks.team.123.completed"))
