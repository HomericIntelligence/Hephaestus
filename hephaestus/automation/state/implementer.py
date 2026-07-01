"""Per-issue implementation state persistence.

Owns the ``states`` dict, the lock guarding it, and the on-disk
``issue-<n>.json`` files under ``state_dir``. Extracted from
:mod:`hephaestus.automation.implementer` as part of the #597 decomposition.

The class deliberately mirrors the inline methods that lived on
``IssueImplementer`` (``_get_or_create_state``, ``_get_state``,
``_save_state``, ``_load_state``) so the coordinator can delegate
without changing call sites.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from .._review_utils import load_state_file, save_state_file
from ..models import ImplementationState

logger = logging.getLogger(__name__)


class ImplementationStateManager:
    """Manages per-issue ``ImplementationState`` with thread-safe access.

    The manager keeps an in-memory dict keyed by issue number and persists
    each state object to ``<state_dir>/issue-<n>.json`` via the secure
    atomic-write helper used by the rest of the automation pipeline.

    Attributes:
        state_dir: Directory holding the on-disk state files.
        states: In-memory state dict (issue number -> state).

    """

    def __init__(self, state_dir: Path) -> None:
        """Initialize the manager.

        Args:
            state_dir: Directory to read/write issue-*.json files. The
                directory is NOT created here — callers (i.e. the
                coordinator) are responsible for ``mkdir(parents=True,
                exist_ok=True)`` so the state file paths remain identical
                to the pre-refactor layout.

        """
        self.state_dir = state_dir
        self.states: dict[int, ImplementationState] = {}
        self._lock = threading.Lock()

    @property
    def lock(self) -> threading.Lock:
        """Expose the internal lock so callers needing compound updates can use it.

        Mirrors the legacy ``IssueImplementer.state_lock`` attribute so
        in-class call sites that wrap ``state.foo = bar`` in
        ``with self.state_lock:`` keep working verbatim.
        """
        return self._lock

    def get_or_create(self, issue_number: int) -> ImplementationState:
        """Return the state for *issue_number*, creating a new one if absent."""
        with self._lock:
            if issue_number not in self.states:
                self.states[issue_number] = ImplementationState(issue_number=issue_number)
            return self.states[issue_number]

    def get(self, issue_number: int) -> ImplementationState | None:
        """Return the state for *issue_number*, or ``None`` if not yet created."""
        with self._lock:
            return self.states.get(issue_number)

    def save(self, state: ImplementationState) -> None:
        """Atomically persist *state* to ``issue-<n>.json`` under ``state_dir``."""
        save_state_file(self.state_dir, "issue", state.issue_number, state)

    def load_all(self) -> None:
        """Load every ``issue-*.json`` file under ``state_dir`` into memory.

        Per-file decode failures are logged and skipped so a single corrupt
        state file cannot prevent the rest of the automation from resuming.
        """
        for state_file in self.state_dir.glob("issue-*.json"):
            try:
                issue_number = int(state_file.stem.removeprefix("issue-"))
            except ValueError:
                logger.error("Failed to load state from %s: invalid issue filename", state_file)
                continue

            state = load_state_file(
                self.state_dir,
                "issue",
                issue_number,
                ImplementationState,
                state_logger=logger,
            )
            if state is None:
                continue

            with self._lock:
                self.states[state.issue_number] = state
            logger.info("Loaded state for issue #%s", state.issue_number)
