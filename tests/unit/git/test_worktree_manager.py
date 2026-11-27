"""Behavior tests for the relocated worktree manager.

Issue #3512 moves ``worktree_manager`` from the automation product layer into
``hephaestus.git``. Product-layer callers must bind the same objects, module
patch points must resolve at the new lookup path, and importing the module must
not load the product layer.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from typing import Any, cast
from unittest.mock import patch

import pytest

from hephaestus.git import worktree_manager


def test_worktree_manager_exposes_its_public_contract() -> None:
    """Verify the library module keeps the public names callers use."""
    assert issubclass(worktree_manager.WorktreeDirtyError, Exception)
    assert issubclass(worktree_manager.RemoteGitRefreshError, RuntimeError)
    assert issubclass(worktree_manager.WorktreeCreationReceiptError, RuntimeError)
    assert issubclass(worktree_manager.BranchWorktreeOwnedError, RuntimeError)
    assert issubclass(worktree_manager.DirectScopeStaleStateError, RuntimeError)
    assert isinstance(worktree_manager.ImplementationWriterAuthority, type)
    assert isinstance(worktree_manager.WorktreeManager, type)
    assert callable(worktree_manager.consume_implementation_writer_authority)
    assert (
        frozenset(
            {
                worktree_manager.ADOPTED_HEAD_VALIDATION_FAILED,
                worktree_manager.ADOPTED_HEAD_TRANSPORT_FAILED,
            }
        )
        == worktree_manager.ADOPTED_HEAD_FAILURES
    )


@pytest.mark.parametrize(
    ("caller", "name"),
    [
        ("hephaestus.automation.source_worktree", "ImplementationWriterAuthority"),
        ("hephaestus.automation.source_worktree", "WorktreeManager"),
        (
            "hephaestus.automation.source_worktree",
            "consume_implementation_writer_authority",
        ),
        ("hephaestus.automation.pipeline.git_cleanup", "WorktreeManager"),
        ("hephaestus.automation.pipeline.worker_pool", "ADOPTED_HEAD_TRANSPORT_FAILED"),
        ("hephaestus.automation.pipeline.worker_pool", "ADOPTED_HEAD_VALIDATION_FAILED"),
        ("hephaestus.automation.pipeline.worker_pool", "BRANCH_WORKTREE_OWNED"),
        ("hephaestus.automation.pipeline.worker_pool", "STALE_PREDICATE_CHECKOUT_PIN"),
        ("hephaestus.automation.pipeline.worker_pool", "BranchWorktreeOwnedError"),
        ("hephaestus.automation.pipeline.worker_pool", "DirectScopeStaleStateError"),
        ("hephaestus.automation.pipeline.worker_pool", "ImplementationWriterAuthority"),
        ("hephaestus.automation.pipeline.worker_pool", "RemoteGitRefreshError"),
        ("hephaestus.automation.pipeline.worker_pool", "WorktreeCreationReceiptError"),
        ("hephaestus.automation.pipeline.worker_pool", "WorktreeManager"),
        (
            "hephaestus.automation.pipeline.stages.implementation.implementation",
            "ADOPTED_HEAD_FAILURES",
        ),
        (
            "hephaestus.automation.pipeline.stages.implementation.implementation",
            "BRANCH_WORKTREE_OWNED",
        ),
    ],
)
def test_product_callers_bind_relocated_objects_by_identity(caller: str, name: str) -> None:
    """Verify product callers bind the same objects as the library module."""
    module = importlib.import_module(caller)
    assert cast(Any, getattr(module, name)) is getattr(worktree_manager, name)


def test_module_patch_points_resolve_at_the_new_path() -> None:
    """Verify retargeted string patch points resolve at the new lookup path."""
    with patch("hephaestus.git.worktree_manager.run") as run_mock:
        assert cast(Any, worktree_manager).run is run_mock
    with patch("hephaestus.git.worktree_manager.get_repo_root") as repo_root_mock:
        assert cast(Any, worktree_manager).get_repo_root is repo_root_mock
    with patch("hephaestus.git.worktree_manager.is_clean_working_tree") as clean_mock:
        assert cast(Any, worktree_manager).is_clean_working_tree is clean_mock
    with patch("hephaestus.git.worktree_manager.operation_file_lock") as lock_mock:
        assert cast(Any, worktree_manager).operation_file_lock is lock_mock
    with patch("hephaestus.git.worktree_manager.run_subprocess") as subprocess_mock:
        assert cast(Any, worktree_manager).run_subprocess is subprocess_mock
    with patch("hephaestus.git.worktree_manager.shutil.rmtree") as rmtree_mock:
        assert cast(Any, worktree_manager).shutil.rmtree is rmtree_mock


def test_worktree_manager_does_not_import_the_product_layer() -> None:
    """Verify importing the module loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.worktree_manager  # noqa: F401\n"
        "loaded = sorted(\n"
        "    name for name in set(sys.modules) - before\n"
        "    if name == 'hephaestus.automation' or name.startswith('hephaestus.automation.')\n"
        ")\n"
        "print('AUTOMATION_LOADED:' + ','.join(loaded))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )
    loaded_line = next(
        line for line in result.stdout.splitlines() if line.startswith("AUTOMATION_LOADED:")
    )
    payload = loaded_line.removeprefix("AUTOMATION_LOADED:")
    loaded = payload.split(",") if payload else []
    assert loaded == [], f"relocated worktree manager loaded product-layer modules: {loaded}"
