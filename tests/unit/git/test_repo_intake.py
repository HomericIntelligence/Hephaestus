"""Behavior tests for the relocated repository-intake control plane.

Issue #3512 moves ``repo_intake`` from the automation product layer into
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

from hephaestus.git import repo_intake


def test_repo_intake_exposes_its_public_contract() -> None:
    """Verify the library module keeps the public names callers use."""
    assert issubclass(repo_intake.RepoIntakeError, RuntimeError)
    assert issubclass(repo_intake.RepoIntakeInUseError, repo_intake.RepoIntakeError)
    assert isinstance(repo_intake.RepoIntakeManager, type)
    assert isinstance(repo_intake.RepoIntakeReceipt, type)
    assert callable(repo_intake.is_full_commit_sha)
    assert callable(repo_intake.is_valid_launch_identity)
    assert callable(repo_intake.recover_abandoned_intakes)
    assert callable(repo_intake.checkout_intake_worker_base)
    assert callable(repo_intake.intake_worker_base)
    assert callable(repo_intake.repository_worker_path_is_valid)
    assert set(repo_intake.__all__) == {
        "RepoIntakeError",
        "RepoIntakeInUseError",
        "RepoIntakeManager",
        "RepoIntakeReceipt",
        "is_full_commit_sha",
        "is_valid_launch_identity",
        "recover_abandoned_intakes",
    }


@pytest.mark.parametrize(
    ("caller", "name"),
    [
        ("hephaestus.automation.pipeline.worker_pool", "RepoIntakeError"),
        ("hephaestus.automation.pipeline.worker_pool", "RepoIntakeManager"),
        ("hephaestus.automation.pipeline.worker_pool", "repository_worker_path_is_valid"),
        ("hephaestus.automation.pipeline.git_cleanup", "checkout_intake_worker_base"),
        ("hephaestus.automation.pipeline.stages.repo.repo", "RepoIntakeReceipt"),
        ("hephaestus.automation.pipeline.coordinator_execution", "RepoIntakeReceipt"),
    ],
)
def test_product_callers_bind_relocated_objects_by_identity(caller: str, name: str) -> None:
    """Verify product callers bind the same objects as the library module."""
    module = importlib.import_module(caller)
    assert cast(Any, getattr(module, name)) is getattr(repo_intake, name)


def test_module_patch_points_resolve_at_the_new_path() -> None:
    """Verify retargeted string patch points resolve at the new lookup path."""
    with patch("hephaestus.git.repo_intake.git_metadata_lock_path") as lock_path:
        assert repo_intake.git_metadata_lock_path is lock_path
    with patch("hephaestus.git.repo_intake.operation_file_lock") as file_lock:
        assert repo_intake.operation_file_lock is file_lock
    with patch("hephaestus.git.repo_intake.os.geteuid", return_value=0) as geteuid:
        assert geteuid() == 0


def test_repo_intake_does_not_import_the_product_layer() -> None:
    """Verify importing the module loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.repo_intake  # noqa: F401\n"
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
    assert loaded == [], f"relocated repo intake loaded product-layer modules: {loaded}"
