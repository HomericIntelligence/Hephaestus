"""Behaviour tests for the relocated Git utility module.

Issue #3512 moves ``git_utils`` from the automation product layer into
``hephaestus.git``. Product-layer callers must bind the same function objects,
and importing the module must not load the product layer.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Any, cast

import pytest

import hephaestus.git.runtime as git_runtime
from hephaestus.git import git_utils


def test_git_utils_exposes_its_public_contract() -> None:
    """Verify the library module keeps the public names callers use."""
    assert callable(git_utils.run)
    assert callable(git_utils.commit_if_changes)
    assert callable(git_utils.push_head_to_branch)
    assert callable(git_utils.rebase_worktree_onto)
    assert git_utils.issue_auto_impl_branch_name(7) == "7-auto-impl"
    assert issubclass(git_utils.BranchPublicationError, RuntimeError)


@pytest.mark.parametrize(
    "name",
    [
        "run",
        "get_repo_root",
        "get_repo_info",
        "get_repo_slug",
        "clear_repo_caches",
        "issue_ref",
        "pr_ref",
    ],
)
def test_git_utils_reexports_runtime_helpers_by_identity(name: str) -> None:
    """Verify the compatibility names still bind the runtime objects."""
    assert cast(Any, getattr(git_utils, name)) is getattr(git_runtime, name)


def test_product_caller_binds_the_relocated_module() -> None:
    """Verify the product-layer worker keeps the same module object."""
    import hephaestus.automation.pipeline.worker_pool as worker_pool

    assert worker_pool.git_utils is git_utils


def test_git_utils_does_not_import_the_product_layer() -> None:
    """Verify importing the module loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.git_utils  # noqa: F401\n"
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
    assert loaded == [], f"relocated git utils loaded product-layer modules: {loaded}"
