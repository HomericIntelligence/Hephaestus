"""Verify BaseReviewer constructor-injection contract (issues #710, #1194)."""

from __future__ import annotations

import inspect
import stat
from pathlib import Path
from unittest.mock import MagicMock

from hephaestus.automation import _reviewer_base
from hephaestus.automation.models import ReviewState
from hephaestus.io import utils as io_utils


def _make_options(max_workers: int = 2) -> object:
    opts = MagicMock()
    opts.max_workers = max_workers
    return opts


def _make_deps(tmp_path: Path) -> dict:
    return {
        "get_repo_root": lambda: tmp_path,
        "worktree_manager_factory": MagicMock(return_value=MagicMock()),
        "status_tracker_factory": MagicMock(return_value=MagicMock()),
        "log_manager_factory": MagicMock(return_value=MagicMock()),
    }


class ConcreteReviewer(_reviewer_base.BaseReviewer):
    """Minimal concrete subclass for testing BaseReviewer's injection contract."""

    def run(self) -> None:
        """Stub implementation to satisfy the ABC abstractmethod contract."""


def test_injection_wires_repo_root(tmp_path: Path) -> None:
    """Constructor injection must wire repo_root from the get_repo_root callable."""
    deps = _make_deps(tmp_path)
    r = ConcreteReviewer(_make_options(), **deps)
    assert r.repo_root == tmp_path


def test_injection_calls_factories(tmp_path: Path) -> None:
    """Constructor must call each factory exactly once, passing max_workers to status."""
    deps = _make_deps(tmp_path)
    ConcreteReviewer(_make_options(max_workers=3), **deps)
    deps["worktree_manager_factory"].assert_called_once_with()
    deps["status_tracker_factory"].assert_called_once_with(3)
    deps["log_manager_factory"].assert_called_once_with()


def test_injected_instances_attached(tmp_path: Path) -> None:
    """Factory return values must be attached to the reviewer instance."""
    deps = _make_deps(tmp_path)
    r = ConcreteReviewer(_make_options(), **deps)
    assert r.worktree_manager is deps["worktree_manager_factory"].return_value
    assert r.status_tracker is deps["status_tracker_factory"].return_value
    assert r.log_manager is deps["log_manager_factory"].return_value


def test_subclass_modules_no_longer_need_reexports() -> None:
    """The importlib monkeypatch contract is gone — no _PATCHABLE_DEPENDENCIES."""
    assert not hasattr(_reviewer_base.BaseReviewer, "_PATCHABLE_DEPENDENCIES")


def test_no_importlib_in_base() -> None:
    """BaseReviewer source must not contain any importlib usage."""
    src = inspect.getsource(_reviewer_base.BaseReviewer)
    assert "importlib" not in src


def test_reviewer_base_uses_canonical_write_secure() -> None:
    """BaseReviewer should import the secure writer from the canonical IO module."""
    assert vars(_reviewer_base)["write_secure"] is io_utils.write_secure


def test_save_state_persists_review_state_with_secure_permissions(tmp_path: Path) -> None:
    """_save_state writes review-<n>.json through the canonical secure writer."""
    deps = _make_deps(tmp_path)
    reviewer = ConcreteReviewer(_make_options(), **deps)
    state = ReviewState(issue_number=1402, pr_number=2402)

    reviewer._save_state(state)

    state_file = tmp_path / "build" / ".issue_implementer" / "review-1402.json"
    restored = ReviewState.model_validate_json(state_file.read_text())
    assert restored.issue_number == 1402
    assert restored.pr_number == 2402
    assert stat.S_IMODE(state_file.stat().st_mode) == 0o600
