"""NATS event models and subject parsing utilities.

Provides :class:`NATSEvent` (the incoming message wrapper) and
:func:`parse_subject` for splitting a dot-separated subject string into
structured components.

Usage::

    from hephaestus.nats.events import NATSEvent, parse_subject

    event = NATSEvent(subject="team.tasks.eng.123.created", data={}, timestamp="", sequence=1)
    parts = parse_subject(event.subject, prefix="team.tasks", n_id_parts=1)
    # SubjectParts(team="eng", task_id="123", verb="created")
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple


class SubjectParts(NamedTuple):
    """Parsed components of a ``<prefix>.<team>.<task_id>.<verb>`` subject.

    Attributes:
        team: Team or service identifier (e.g., ``scylla``).
        task_id: Task identifier.
        verb: Action verb (e.g., ``created``, ``updated``, ``completed``).

    """

    team: str
    task_id: str
    verb: str


@dataclass
class NATSEvent:
    """Incoming NATS JetStream message payload.

    Attributes:
        subject: Full NATS subject string.
        data: Decoded JSON message body.
        timestamp: ISO-8601 timestamp of the message.
        sequence: JetStream sequence number.

    """

    subject: str
    data: dict[str, Any]
    timestamp: str
    sequence: int

    def __post_init__(self) -> None:
        """Validate the JetStream sequence number.

        Raises:
            ValueError: If ``sequence`` is negative.

        """
        if self.sequence < 0:
            raise ValueError(f"sequence must be >= 0, got {self.sequence}")


def parse_subject(subject: str, prefix: str = "hi.tasks") -> SubjectParts:
    """Parse a ``<prefix>.<team>.<task_id>.<verb>`` subject into components.

    The subject is expected to have exactly ``len(prefix.split(".")) + 3``
    dot-separated parts: the prefix segments, a team, a task ID, and a verb.

    Args:
        subject: Full NATS subject string.
        prefix: Dot-separated prefix before ``<team>.<task_id>.<verb>``.
            Defaults to ``"hi.tasks"``.

    Returns:
        :class:`SubjectParts` with team, task_id, and verb.

    Raises:
        ValueError: If the subject does not match the expected structure.

    """
    prefix_parts = prefix.split(".")
    expected_total = len(prefix_parts) + 3
    parts = subject.split(".")
    if len(parts) != expected_total:
        raise ValueError(
            f"Expected subject with {expected_total} parts "
            f"({prefix}.<team>.<task_id>.<verb>), "
            f"got {len(parts)} parts: {subject!r}"
        )
    offset = len(prefix_parts)
    return SubjectParts(team=parts[offset], task_id=parts[offset + 1], verb=parts[offset + 2])
