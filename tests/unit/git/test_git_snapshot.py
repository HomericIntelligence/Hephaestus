"""Behaviour tests for the hephaestus.git snapshot module.

Issue #3519 moved the bounded Git capture helpers from the automation product
layer into ``hephaestus.git.snapshot``. The module must import from the library
package, and its timeout helper must be the same object as the helper in
``hephaestus.git.runtime``. The capture path relies on that single binding.
"""

from __future__ import annotations

import subprocess
import sys


def test_snapshot_imports_from_the_library_package() -> None:
    """Verify `hephaestus.git.snapshot` imports and exposes its public helpers."""
    import hephaestus.git.snapshot as snapshot

    assert callable(snapshot.run_bounded_git_output)
    assert callable(snapshot.path_content_identity)


def test_timeout_helper_is_the_runtime_object() -> None:
    """Verify `hephaestus.git.snapshot` uses the one runtime timeout helper.

    A second definition would let a deadline change reach only one caller.
    """
    import hephaestus.git.runtime as runtime
    import hephaestus.git.snapshot as snapshot

    imported = vars(snapshot)["remaining_operation_timeout"]
    assert imported is runtime.remaining_operation_timeout


def test_snapshot_does_not_import_the_product_layer() -> None:
    """Verify importing `hephaestus.git.snapshot` loads no product-layer module.

    The assertion reads the import graph in `sys.modules`, not the module's
    own name. Subprocess isolation keeps the check meaningful because a
    pytest session can already hold product-layer modules from other tests.
    """
    code = (
        "import sys\n"
        "before = set(sys.modules)\n"
        "import hephaestus.git.snapshot  # noqa: F401\n"
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
    assert loaded == [], f"`hephaestus.git.snapshot` loaded product-layer modules: {loaded}"
