"""Operator-facing shutdown guidance for the queue coordinator."""

from __future__ import annotations

import signal


def shutdown_signal_message(signum: int, grace_s: float, *, immediate: bool) -> str:
    """Return guidance for the coordinator's cooperative shutdown."""
    signal_name = "Ctrl+C" if signum == signal.SIGINT else f"signal {signum}"
    if immediate:
        return f"second {signal_name} received: forcing immediate teardown"
    instruction = "press Ctrl+C again" if signum == signal.SIGINT else "send signal again"
    return (
        f"{signal_name} received: timed graceful shutdown started ({grace_s:.0f}s); "
        f"{instruction} to force teardown"
    )
