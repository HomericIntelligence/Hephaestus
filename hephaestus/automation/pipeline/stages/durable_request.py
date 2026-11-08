"""The one home of the pending GitHub request payload entry.

Each stage that dispatches a correlated GitHub job stores its request in
``item.payload`` before the queue push, then compares the live request with
the job receipt or with the next request. This module owns the payload key and
the two read predicates. Each stage still owns the pop-on-receipt timing, its
receipt keys, its terminal reasons, and its error classification.
"""

from __future__ import annotations

from typing import Final

from ..work_item import WorkItem

#: Payload key that holds the request of the in-flight GitHub job.
PENDING_GITHUB_REQUEST: Final[str] = "_pending_github_request"


def pending_request(item: WorkItem) -> object | None:
    """Return the in-flight GitHub request, or ``None`` when none is pending."""
    return item.payload.get(PENDING_GITHUB_REQUEST)


def pending_request_matches(item: WorkItem, request: object) -> bool:
    """Return whether a live pending request equals the supplied request."""
    pending = pending_request(item)
    return pending is not None and pending == request


__all__ = [
    "PENDING_GITHUB_REQUEST",
    "pending_request",
    "pending_request_matches",
]
