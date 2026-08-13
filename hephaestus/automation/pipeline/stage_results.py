"""Provider-neutral state-machine results shared by pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Continue:
    """Advance to the next state without requesting a job."""

    next_state: str


@dataclass(frozen=True)
class JobRequest:
    """Request one frozen job and name its completion state."""

    job: Any
    on_done_state: str
