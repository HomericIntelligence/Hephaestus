"""Behavior tests for the relocated source-worktree module.

Issue #3512 moves ``source_worktree`` from the automation product layer into
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

from hephaestus.git import source_worktree


def test_source_worktree_exposes_its_public_contract() -> None:
    """Verify the library module keeps the public names callers use."""
    assert issubclass(source_worktree.SourceWorkspaceError, RuntimeError)
    assert issubclass(
        source_worktree.SourceWorkspaceCreationError, source_worktree.SourceWorkspaceError
    )
    assert issubclass(
        source_worktree.SourceWorkspaceTerminalError,
        source_worktree.SourceWorkspaceCreationError,
    )
    assert issubclass(
        source_worktree.SourceWorkspacePreparationError,
        source_worktree.SourceWorkspaceError,
    )
    assert isinstance(source_worktree.SourceWorkspaceManager, type)
    assert isinstance(source_worktree.FleetAttemptFence, type)
    assert isinstance(source_worktree.SourceWorkspaceReceipt, type)
    assert isinstance(source_worktree.SourceWorkspaceRecovery, type)
    assert isinstance(source_worktree._PreparationDeadline, type)
    assert callable(source_worktree.normalize_source_workspace_creation_failure)
    assert source_worktree.SourceWorkspaceRecoveryKind.DIRTY_WORKTREE.value == "dirty_worktree"
    assert source_worktree.SourceWorkspaceCreationFailure.UNKNOWN.value == "unknown"
    assert source_worktree.SourceWorkspacePreparationCause.GIT_TIMEOUT.value == "git_timeout"


@pytest.mark.parametrize(
    ("caller", "name"),
    [
        ("hephaestus.automation.host_capabilities", "_PreparationDeadline"),
        ("hephaestus.automation.athena_jobs", "SourceWorkspaceManager"),
        ("hephaestus.automation.pipeline.auxiliary_worker_pool", "_PreparationDeadline"),
        ("hephaestus.automation.pipeline.diagnostics", "SourceWorkspaceRecoveryKind"),
        ("hephaestus.automation.pipeline.fleet_execution", "FleetAttemptFence"),
        ("hephaestus.automation.pipeline.fleet_execution", "SourceWorkspaceReceipt"),
        ("hephaestus.automation.pipeline.git_cleanup", "SourceWorkspaceError"),
        ("hephaestus.automation.pipeline.git_cleanup", "SourceWorkspaceManager"),
        ("hephaestus.automation.pipeline.job_failures", "SourceWorkspacePreparationCause"),
        ("hephaestus.automation.pipeline.stage_base", "_PreparationDeadline"),
        (
            "hephaestus.automation.pipeline.stages.planning.planning",
            "SourceWorkspacePreparationError",
        ),
        (
            "hephaestus.automation.pipeline.stages.pr_review.pr_review_jobs",
            "SourceWorkspaceError",
        ),
        ("hephaestus.automation.pipeline.worker_pool", "SourceWorkspaceCreationError"),
        ("hephaestus.automation.pipeline.worker_pool", "SourceWorkspaceCreationFailure"),
        ("hephaestus.automation.pipeline.worker_pool", "SourceWorkspaceReceipt"),
        ("hephaestus.automation.pipeline.worker_pool", "SourceWorkspaceRecovery"),
        ("hephaestus.automation.pipeline.worker_pool", "_terminal_json_object"),
        ("hephaestus.automation.pipeline.worker_pool", "_terminal_read_bytes"),
        (
            "hephaestus.automation.pipeline.worker_pool",
            "normalize_source_workspace_creation_failure",
        ),
    ],
)
def test_product_callers_bind_relocated_objects_by_identity(caller: str, name: str) -> None:
    """Verify product callers bind the same objects as the library module."""
    module = importlib.import_module(caller)
    assert cast(Any, getattr(module, name)) is getattr(source_worktree, name)


def test_module_patch_points_resolve_at_the_new_path() -> None:
    """Verify retargeted string patch points resolve at the new lookup path."""
    with patch("hephaestus.git.source_worktree.run_subprocess") as subprocess_mock:
        assert cast(Any, source_worktree).run_subprocess is subprocess_mock
    with patch("hephaestus.git.source_worktree.file_lock") as lock_mock:
        assert cast(Any, source_worktree).file_lock is lock_mock
    with patch("hephaestus.git.source_worktree.write_secure") as secure_mock:
        assert cast(Any, source_worktree).write_secure is secure_mock
    with patch(
        "hephaestus.git.source_worktree.consume_implementation_writer_authority"
    ) as authority_mock:
        assert cast(Any, source_worktree).consume_implementation_writer_authority is authority_mock
    with patch.object(source_worktree, "_git") as git_mock:
        assert source_worktree._git is git_mock


def test_source_worktree_does_not_import_the_product_layer() -> None:
    """Verify importing the module loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.source_worktree  # noqa: F401\n"
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
    assert loaded == [], f"relocated source worktree loaded product-layer modules: {loaded}"
