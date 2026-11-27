"""Behaviour tests for the relocated Git commit runtime module.

Issue #3512 moves ``commit_runtime`` from the automation product layer into
``hephaestus.git``. Product-layer callers must bind the same function objects,
and importing the module must not load the product layer.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hephaestus.git import commit_runtime


def test_commit_runtime_exposes_its_public_contract() -> None:
    """Verify the library module keeps the public names callers use."""
    assert callable(commit_runtime.commit_changes)
    metadata = commit_runtime.CommitIssueMetadata(number=7, title="feat: add helper", body="body")
    assert metadata.number == 7
    assert metadata.title == "feat: add helper"


def test_commit_metadata_rejects_an_invalid_issue_number() -> None:
    """Verify the relocated validation still rejects a nonpositive number."""
    with pytest.raises(ValueError):
        commit_runtime.CommitIssueMetadata(number=0, title="feat: add helper", body="")


def test_product_caller_binds_the_relocated_commit_changes() -> None:
    """Verify the product-layer helper keeps the same commit function object."""
    from hephaestus.automation import git_utils

    assert git_utils._commit_changes is commit_runtime.commit_changes


def test_commit_runtime_does_not_import_the_product_layer() -> None:
    """Verify importing the module loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.commit_runtime  # noqa: F401\n"
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
    assert loaded == [], f"relocated commit runtime loaded product-layer modules: {loaded}"
