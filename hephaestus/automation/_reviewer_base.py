"""Shared scaffolding for the PR reviewer + address-review CLIs.

Issue #599: ``PRReviewer`` and ``AddressReviewer`` previously duplicated
their ``__init__``, ``_log``, ``_fail`` / ``_fail_review``, and review-state
loading logic. This module hosts the common pieces as ``BaseReviewer``,
which both concrete classes now subclass.

Subclasses own only the work-specific methods (``_review_pr`` for
``PRReviewer``; ``_address_issue`` for ``AddressReviewer``).
"""

from __future__ import annotations

import importlib
import logging
import threading
from pathlib import Path
from typing import Any

from ._review_utils import instance_log
from .curses_ui import CursesUI, ThreadLogManager
from .git_utils import issue_ref
from .github_api import write_secure
from .models import ReviewPhase, ReviewState, WorkerResult
from .status_tracker import StatusTracker
from .worktree_manager import WorktreeManager

logger = logging.getLogger(__name__)


_MISSING = object()


def _resolve_from_subclass_module(cls: type, name: str) -> Any:
    """Look up ``name`` from the module that defines ``cls``.

    The reviewer subclasses re-export ``WorktreeManager``, ``StatusTracker``,
    and ``ThreadLogManager`` from their own modules so tests can patch them
    with ``patch("hephaestus.automation.<module>.<Name>")``. Looking these
    up dynamically here preserves that test seam — patching the subclass
    module still wins.

    Raises:
        TypeError: If the subclass module does not re-export ``name``. The
            error names the offending subclass and module so a future author
            of a third ``BaseReviewer`` subclass gets an actionable message
            instead of a bare ``AttributeError``.

    """
    module = importlib.import_module(cls.__module__)
    obj = getattr(module, name, _MISSING)
    if obj is _MISSING:
        raise TypeError(
            f"{cls.__qualname__} must re-export {name!r} in its own module "
            f"({cls.__module__}) for BaseReviewer test-patch compatibility. "
            f"Add `from .{name.lower()}_module import {name}  # noqa: F401` "
            f"or equivalent to {cls.__module__}."
        )
    return obj


class BaseReviewer:
    """Shared scaffolding for the reviewer CLIs.

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

    def __init__(self, options: Any) -> None:
        """Initialize the shared reviewer scaffolding.

        Args:
            options: A subclass-specific options model. Must expose
                ``max_workers`` (other attributes are read by subclasses).

        """
        self.options = options
        # Resolve from the subclass's module so tests can patch them via
        # ``patch("hephaestus.automation.<module>.<Name>")``.
        get_repo_root_fn = _resolve_from_subclass_module(type(self), "get_repo_root")
        self.repo_root: Path = Path(get_repo_root_fn())
        self.state_dir: Path = self.repo_root / "build" / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        worktree_manager_cls = _resolve_from_subclass_module(type(self), "WorktreeManager")
        status_tracker_cls = _resolve_from_subclass_module(type(self), "StatusTracker")
        thread_log_manager_cls = _resolve_from_subclass_module(type(self), "ThreadLogManager")

        self.worktree_manager: WorktreeManager = worktree_manager_cls()
        self.status_tracker: StatusTracker = status_tracker_cls(options.max_workers)
        self.log_manager: ThreadLogManager = thread_log_manager_cls()

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
        state_file = self.state_dir / f"review-{state.issue_number}.json"
        write_secure(state_file, state.model_dump_json(indent=2))

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
        state_file = self.state_dir / f"review-{issue_number}.json"
        if not state_file.exists():
            return None
        try:
            return ReviewState.model_validate_json(state_file.read_text())
        except Exception as exc:
            # Log under the subclass module's logger so observability tooling
            # that filters on logger name attributes this to pr_reviewer /
            # address_review (same behavior as before the BaseReviewer split).
            subclass_logger = logging.getLogger(type(self).__module__)
            subclass_logger.warning("Malformed review state for issue #%d (%s)", issue_number, exc)
            return None
