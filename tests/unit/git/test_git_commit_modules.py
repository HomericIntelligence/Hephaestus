"""Behaviour tests for the relocated Git commit-policy modules.

Issue #3512 moved ``commit_paths`` and ``commit_policy`` from the automation
product layer into ``hephaestus.git``. Product-layer callers must see the
same function and class objects, and importing the modules must not load the
product layer.
"""

from __future__ import annotations

import subprocess
import sys


def test_commit_path_helpers_are_callable_from_the_library() -> None:
    """Verify `hephaestus.git.commit_paths` exposes its public helpers."""
    from hephaestus.git.commit_paths import (
        CommitPaths,
        is_bounded_commit_paths,
        parse_porcelain_status,
        select_commit_paths,
    )

    paths = CommitPaths(("a.txt",), ())
    assert is_bounded_commit_paths(paths, max_paths=1, max_bytes=64)
    assert parse_porcelain_status("M  a.txt\0") == (("M ", "a.txt"),)
    selected = select_commit_paths((("M ", "a.txt"),), None)
    assert selected.add_paths == ("a.txt",)
    assert selected.update_paths == ()


def test_commit_policy_helpers_are_callable_from_the_library() -> None:
    """Verify `hephaestus.git.commit_policy` exposes its public helpers."""
    from hephaestus.git.commit_policy import (
        ALLOWED_CONVENTIONAL_TYPES,
        normalize_conventional_type,
        normalize_strict_conventional_title,
    )

    assert "feat" in ALLOWED_CONVENTIONAL_TYPES
    assert normalize_conventional_type("feat: add helper") == "feat: add helper"
    assert normalize_strict_conventional_title("fix(core): repair") == "fix(core): repair"


def test_product_callers_bind_the_relocated_objects() -> None:
    """Verify consumers keep the same object as the relocated definition."""
    from hephaestus.automation.pipeline.stages.implementation import implementation
    from hephaestus.git import commit_runtime
    from hephaestus.git.commit_paths import parse_porcelain_status
    from hephaestus.git.commit_policy import normalize_strict_conventional_title

    assert commit_runtime.parse_porcelain_status is parse_porcelain_status
    assert implementation.normalize_strict_conventional_title is normalize_strict_conventional_title


def test_commit_modules_do_not_import_the_product_layer() -> None:
    """Verify importing both modules loads no automation module in a clean process."""
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.commit_paths  # noqa: F401\n"
        "import hephaestus.git.commit_policy  # noqa: F401\n"
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
    assert loaded == [], f"relocated commit modules loaded product-layer modules: {loaded}"
