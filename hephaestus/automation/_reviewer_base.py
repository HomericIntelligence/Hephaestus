"""Shared scaffolding for the PR reviewer + address-review CLIs.

Issue #599: ``PRReviewer`` and ``AddressReviewer`` previously duplicated
their ``__init__``, ``_log``, ``_fail`` / ``_fail_review``, and review-state
loading logic. This module hosts the common pieces as ``BaseReviewer``,
which both concrete classes now subclass.

Subclasses own only the work-specific methods (``_review_pr`` for
``PRReviewer``; ``_address_issue`` for ``AddressReviewer``).
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ._review_utils import ensure_state_dir, instance_log, load_state_file, save_state_file
from .curses_ui import CursesUI, ThreadLogManager
from .git_utils import get_repo_root as _default_get_repo_root, issue_ref
from .models import ReviewPhase, ReviewState, WorkerResult
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


class BaseReviewer(ABC):
    """Shared scaffolding for the reviewer CLIs.

    Collaborators are injected via constructor parameters (DIP-compliant).
    Each defaults to the real production implementation so production call
    sites need no change.  Tests pass lightweight fakes directly without
    monkeypatching module globals.

    Owns:
        - ``options``: subclass-specific options model (duck-typed; must expose
          ``max_workers``).
        - ``repo_root`` + ``state_dir``: filesystem layout.
        - ``worktree_manager``: shared ``WorktreeManager``.
        - ``status_tracker``: parallel-worker slot tracker.
        - ``log_manager``: per-thread UI log buffer.
        - ``states``: in-memory ``ReviewState`` cache keyed by issue number.
        - ``state_lock``: serializes mutation of ``states``.
        - ``ui``: optional ``CursesUI`` (set by subclass ``run()``).

    Concrete subclasses override the work-specific methods only.
    """

    def __init__(
        self,
        options: Any,
        *,
        get_repo_root: Callable[[], Any] = _default_get_repo_root,
        worktree_manager_factory: Callable[[], WorktreeManager] = WorktreeManager,
        status_tracker_factory: Callable[[int], StatusTracker] = StatusTracker,
        log_manager_factory: Callable[[], ThreadLogManager] = ThreadLogManager,
    ) -> None:
        """Initialize the shared reviewer scaffolding.

        Args:
            options: A subclass-specific options model. Must expose
                ``max_workers`` (other attributes are read by subclasses).
            get_repo_root: Callable returning the repo root path. Defaults to
                :func:`.git_utils.get_repo_root`.
            worktree_manager_factory: Zero-arg callable returning a
                :class:`WorktreeManager`. Defaults to :class:`WorktreeManager`.
            status_tracker_factory: One-arg callable accepting ``num_slots``
                and returning a :class:`StatusTracker`. Defaults to
                :class:`StatusTracker`.
            log_manager_factory: Zero-arg callable returning a
                :class:`ThreadLogManager`. Defaults to :class:`ThreadLogManager`.

        """
        self.options = options
        self.repo_root: Path = Path(get_repo_root())
        self.state_dir: Path = ensure_state_dir(self.repo_root)

        self.worktree_manager: WorktreeManager = worktree_manager_factory()
        self.status_tracker: StatusTracker = status_tracker_factory(options.max_workers)
        self.log_manager: ThreadLogManager = log_manager_factory()

        self.states: dict[int, ReviewState] = {}
        self.state_lock = threading.Lock()

        self.ui: CursesUI | None = None

    def _log(self, level: str, msg: str, thread_id: int | None = None) -> None:
        """Log to both module logger and per-thread UI buffer.

        Subclasses may override to substitute their own module logger so that
        the log record's ``name`` matches the subclass module.
        """
        instance_log(self.log_manager, level, msg, thread_id, caller_logger=logger)

    def _save_state(self, state: ReviewState) -> None:
        """Persist a ``ReviewState`` to ``review-<issue_number>.json``.

        Both reviewer CLIs use this same filename layout.

        Args:
            state: The ``ReviewState`` to persist.

        """
        save_state_file(self.state_dir, "review", state.issue_number, state)

    def _fail(
        self,
        issue_number: int,
        error_msg: str,
        slot_id: int,
    ) -> WorkerResult:
        """Record a worker failure and return a failed ``WorkerResult``.

        Updates the status tracker, marks any in-memory state ``FAILED``,
        persists it, and returns a ``WorkerResult(success=False)``.

        Args:
            issue_number: GitHub issue number.
            error_msg: Human-readable error description.
            slot_id: Worker slot ID for status updates.

        Returns:
            ``WorkerResult`` with ``success=False`` and ``error=error_msg``.

        """
        self.status_tracker.update_slot(
            slot_id, f"{issue_ref(issue_number)}: FAILED - {error_msg[:50]}"
        )
        err_state = self.states.get(issue_number)
        if err_state:
            with self.state_lock:
                err_state.phase = ReviewPhase.FAILED
                err_state.error = error_msg
            self._save_state(err_state)
        return WorkerResult(issue_number=issue_number, success=False, error=error_msg)

    def _load_review_state_from_disk(self, issue_number: int) -> ReviewState | None:
        """Load a ``ReviewState`` from disk, or return ``None`` if absent / invalid.

        Args:
            issue_number: GitHub issue number.

        Returns:
            ``ReviewState`` if the file exists and parses, else ``None``.

        """
        # Log under the subclass module's logger so observability tooling
        # that filters on logger name attributes this to pr_reviewer /
        # address_review (same behavior as before the BaseReviewer split).
        subclass_logger = logging.getLogger(type(self).__module__)
        return load_state_file(
            self.state_dir,
            "review",
            issue_number,
            ReviewState,
            state_logger=subclass_logger,
        )

    @abstractmethod
    def run(self) -> Any:
        """Execute the review loop and return per-issue results."""
