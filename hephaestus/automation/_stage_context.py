"""Shared per-issue stage context for the implementation pipeline phases.

The #712 decomposition splits :class:`ImplementationPhaseRunner` into five
single-responsibility phase collaborators (plan / implement / review /
PR-create / follow-up). Every phase needs the same handful of shared
references — the parent :class:`~hephaestus.automation.implementer.IssueImplementer`,
its options, state dir, repo root, trackers — plus a back-reference to the
:class:`ImplementationPhaseRunner` so cross-phase dispatch can flow through
the coordinator's shim methods (preserving the
``patch.object(impl, "_method", ...)`` test contract after extraction).

``StageContext`` is the single object passed to every phase constructor. It
holds ``impl`` and ``runner`` and re-exposes the runner's convenience
accessors so phase method bodies keep reading ``self.options`` /
``self.state_dir`` / ``self.impl`` unchanged after they move out of the
runner. Phase classes mix these in via :class:`StageMixin`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .implementer import IssueImplementer
    from .implementer_phase_runner import ImplementationPhaseRunner


@dataclass
class StageContext:
    """Shared references handed to every implementation phase.

    Attributes:
        impl: Parent ``IssueImplementer``. Held by reference; phases read
            ``impl.options`` / ``impl.state_dir`` / ``impl.repo_root`` /
            ``impl.worktree_manager`` / ``impl.status_tracker`` /
            ``impl.state_mgr`` and call the ``_log`` / ``_get_state`` /
            ``_save_state`` helpers on it.
        runner: The :class:`ImplementationPhaseRunner` that owns this
            context. Phases dispatch cross-phase work through the runner's
            public delegator methods so the test-patch contract on ``impl``
            (which forwards to the runner) keeps intercepting every callsite.

    """

    impl: IssueImplementer
    runner: ImplementationPhaseRunner

    @property
    def options(self) -> Any:
        """Return the parent ImplementerOptions."""
        return self.impl.options

    @property
    def state_dir(self) -> Path:
        """Return the state directory used for on-disk artifacts."""
        return self.impl.state_dir

    @property
    def repo_root(self) -> Path:
        """Return the repository root used as default CWD."""
        return self.impl.repo_root

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
        return self.impl.state_mgr.lock

    @property
    def impl_module(self) -> ModuleType:
        """Return the ``hephaestus.automation.implementer`` module.

        Resolves the patchable-symbol surface documented by the "Test-Patch
        Contract" table in :mod:`.implementer` (``is_plan_review_go``,
        ``fetch_issue_info``, ``invoke_claude_with_session``, ``get_repo_slug``,
        ``find_pr_for_issue``, ``review_state``, ``AGENT_IMPLEMENTER``, …) so
        that tests which ``patch("hephaestus.automation.implementer.X", ...)``
        keep working after the call sites moved into the phase modules.
        ``patch("…implementer.X", …)`` intercepts attribute lookup here.

        Cycle constraint: :mod:`.implementer` eagerly imports
        ``ImplementationPhaseRunner`` (which imports this module) at module top,
        so a top-level reverse import would create a partial-module crash. The
        inline ``from . import implementer`` below is therefore required — it
        fires only at attribute-access time, by which point
        ``sys.modules["hephaestus.automation.implementer"]`` is fully populated.
        Return type ``ModuleType`` lets mypy check the property signature;
        attribute access through the returned module remains ``Any`` (mypy's
        ``ModuleType.__getattr__`` stub returns ``Any``), which is acceptable
        here because the patchable surface is enumerated in the docstring
        table — that table, not mypy, is the contract.
        """
        from . import implementer as _impl_mod  # cycle-safe; see docstring

        return _impl_mod


class StageMixin:
    """Convenience-accessor mixin for phase classes.

    Each phase stores its :class:`StageContext` as ``self.ctx`` and inherits
    the runner's accessor names (``self.options``, ``self.state_dir``,
    ``self.impl``, ``self._impl_module``, …) so the method bodies that moved
    out of :class:`ImplementationPhaseRunner` keep reading them unchanged.
    """

    ctx: StageContext

    @property
    def impl(self) -> IssueImplementer:
        """Return the parent ``IssueImplementer``."""
        return self.ctx.impl

    @property
    def runner(self) -> ImplementationPhaseRunner:
        """Return the owning :class:`ImplementationPhaseRunner`."""
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

    @property
    def _impl_module(self) -> Any:
        """Return the ``hephaestus.automation.implementer`` module."""
        return self.ctx.impl_module
