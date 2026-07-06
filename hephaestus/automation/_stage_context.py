"""Shared per-issue stage context for the implementation pipeline phases.

The #712 decomposition split the implementation flow into five
single-responsibility phase collaborators (plan / implement / review /
PR-create / follow-up). Every phase needs the same handful of shared
references — a parent implementer-like object, its options, state dir, repo
root, trackers — plus a ``runner`` back-reference through which cross-phase
dispatch flows.

The epic #1809 pipeline conversion re-housed this control flow into the
pipeline stages (``pipeline/stages/implementation.py`` + ``pr_review.py``) and
removed the legacy per-issue phase runner. ``StageContext`` remains the
shared value object these phase collaborators are built around; ``impl`` and
``runner`` are now loosely typed (``Any``) because there is no single owning
coordinator class anymore.

``StageContext`` is the single object passed to every phase constructor. It
holds ``impl`` and ``runner`` and re-exposes the convenience accessors so phase
method bodies keep reading ``self.options`` / ``self.state_dir`` / ``self.impl``
unchanged. Phase classes mix these in via :class:`StageMixin`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast


@dataclass
class StageContext:
    """Shared references handed to every implementation phase.

    Attributes:
        impl: Parent implementer-like object. Held by reference; phases read
            ``impl.options`` / ``impl.state_dir`` / ``impl.repo_root`` /
            ``impl.worktree_manager`` / ``impl.status_tracker`` /
            ``impl.state_mgr`` and call the ``_log`` / ``_get_state`` /
            ``_save_state`` helpers on it. Loosely typed (``Any``): the phase
            collaborators reach through to helper methods that the legacy phase
            runner used to provide, so a static class type would over-constrain
            them.
        runner: Back-reference through which phases dispatch cross-phase work.
            Loosely typed (``Any``) since the legacy owning coordinator class
            was removed with the epic #1809 pipeline conversion.

    """

    impl: Any
    runner: Any

    @property
    def options(self) -> Any:
        """Return the parent ImplementerOptions."""
        return self.impl.options

    @property
    def state_dir(self) -> Path:
        """Return the state directory used for on-disk artifacts."""
        return cast(Path, self.impl.state_dir)

    @property
    def repo_root(self) -> Path:
        """Return the repository root used as default CWD."""
        return cast(Path, self.impl.repo_root)

    @property
    def status_tracker(self) -> Any:
        """Return the shared :class:`StatusTracker`."""
        return self.impl.status_tracker

    @property
    def worktree_manager(self) -> Any:
        """Return the shared :class:`WorktreeManager`."""
        return self.impl.worktree_manager

    @property
    def state_lock(self) -> threading.Lock:
        """Return the lock guarding the state manager's in-memory dict."""
        return cast(threading.Lock, self.impl.state_mgr.lock)


class StageMixin:
    """Convenience-accessor mixin for phase classes.

    Each phase stores its :class:`StageContext` as ``self.ctx`` and inherits
    the shared accessor names (``self.options``, ``self.state_dir``,
    ``self.impl``, …) so the phase method bodies keep reading them unchanged.
    """

    ctx: StageContext

    @property
    def impl(self) -> Any:
        """Return the parent implementer-like object."""
        return self.ctx.impl

    @property
    def runner(self) -> Any:
        """Return the cross-phase dispatch back-reference."""
        return self.ctx.runner

    @property
    def options(self) -> Any:
        """Return the parent ImplementerOptions."""
        return self.ctx.options

    @property
    def state_dir(self) -> Path:
        """Return the state directory used for on-disk artifacts."""
        return self.ctx.state_dir

    @property
    def repo_root(self) -> Path:
        """Return the repository root used as default CWD."""
        return self.ctx.repo_root

    @property
    def status_tracker(self) -> Any:
        """Return the shared :class:`StatusTracker`."""
        return self.ctx.status_tracker

    @property
    def worktree_manager(self) -> Any:
        """Return the shared :class:`WorktreeManager`."""
        return self.ctx.worktree_manager

    @property
    def state_lock(self) -> threading.Lock:
        """Return the lock guarding the state manager's in-memory dict."""
        return self.ctx.state_lock
