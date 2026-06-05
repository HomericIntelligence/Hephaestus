"""Tests for the inner polling timeout behavior of _subscribe_loop (#753)."""
from __future__ import annotations

import inspect

from hephaestus.nats import subscriber


def test_polling_loop_source_code_does_not_use_asyncio_wait_for() -> None:
    """Regression: source code must not contain asyncio.wait_for around next_msg (#753)."""
    source = inspect.getsource(subscriber.NATSSubscriberThread._subscribe_loop)
    assert (
        "asyncio.wait_for" not in source
    ), "asyncio.wait_for must not be used to wrap next_msg (issue #753)"


def test_polling_loop_uses_only_next_msg_timeout() -> None:
    """The inner timeout=0.5 on next_msg is the only timeout mechanism."""
    source = inspect.getsource(subscriber.NATSSubscriberThread._subscribe_loop)
    # Check that next_msg is called with timeout=0.5
    assert (
        "next_msg(timeout=0.5)" in source
    ), "next_msg must be called with timeout=0.5"
    # Check the except clause catches TimeoutError only
    assert "except TimeoutError:" in source, "except clause must catch TimeoutError"


def test_except_clause_not_redundant() -> None:
    """The except clause should not catch both asyncio.TimeoutError and TimeoutError."""
    source = inspect.getsource(subscriber.NATSSubscriberThread._subscribe_loop)
    # On Python 3.10+, catching both is redundant
    assert (
        "except (asyncio.TimeoutError, TimeoutError):" not in source
    ), "except clause must not catch both asyncio.TimeoutError and TimeoutError"
    assert (
        "except TimeoutError:" in source
    ), "except clause should catch TimeoutError only"
