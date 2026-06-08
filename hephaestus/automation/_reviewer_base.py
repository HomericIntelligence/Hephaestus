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

    Implements the test-seam contract documented on
    :class:`BaseReviewer` (see :attr:`BaseReviewer._PATCHABLE_DEPENDENCIES`).
    Tracked for removal under issue #710 (constructor-injection refactor).

    Raises:
        TypeError: If the subclass module does not re-export ``name``. The
            error names the offending subclass, module, and the test-seam
            contract so a future author of a third ``BaseReviewer`` subclass
            gets an actionable message instead of a bare ``AttributeError``.

    """
    module = importlib.import_module(cls.__module__)
    obj = getattr(module, name, _MISSING)
    if obj is _MISSING:
        raise TypeError(
            f"{cls.__qualname__} must re-export {name!r} in its own module "
            f"({cls.__module__}) for BaseReviewer's test-seam contract "
            f"(see BaseReviewer._PATCHABLE_DEPENDENCIES, issue #710). "
            f"Add `from .<source_module> import {name}  # noqa: F401` "
            f"to {cls.__module__}."
        )
    return obj


class BaseReviewer:
    """Shared scaffolding for the reviewer CLIs.

    Test-seam contract (see issues #806, #710):
        Concrete subclasses MUST re-export every symbol in
        :attr:`_PATCHABLE_DEPENDENCIES` from their own module so unit tests
        can patch them via ``patch("hephaestus.automation.<subclass>.<Name>")``.
        ``__init__`` resolves these symbols dynamically from the subclass
        module to honor that patch surface.

        This inverts the natural ``base → subclass`` import direction; it is
        accepted as a deliberate test-seam until the dependency-injection
        refactor in #710 lands. Adding a fourth subclass? Re-export the four
        names below and you are done.

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

    # TODO(#710): replace this dynamic test-seam with constructor injection.
    _PATCHABLE_DEPENDENCIES: tuple[str, ...] = (
        "get_repo_root",
        "WorktreeManager",
        "StatusTracker",
        "ThreadLogManager",
    )

    def __init__(self, options: Any) -> None:
        """Initialize the shared reviewer scaffolding.

        Args:
            options: A subclass-specific options model. Must expose
                ``max_workers`` (other attributes are read by subclasses).

        """
        self.options = options
        resolved = {
            name: _resolve_from_subclass_module(type(self), name)
            for name in self._PATCHABLE_DEPENDENCIES
        }
        self.repo_root: Path = Path(resolved["get_repo_root"]())
        self.state_dir: Path = self.repo_root / "build" / ".issue_implementer"
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.worktree_manager: WorktreeManager = resolved["WorktreeManager"]()
        self.status_tracker: StatusTracker = resolved["StatusTracker"](options.max_workers)
        self.log_manager: ThreadLogManager = resolved["ThreadLogManager"]()

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
