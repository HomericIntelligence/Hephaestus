"""Behaviour tests for the hephaestus.git snapshot module.

Issue #3519 moved the bounded Git capture helpers from the automation product
layer into ``hephaestus.git.snapshot``. The module must import from the library
package, and its timeout helper must be the same object as the helper in
``hephaestus.git.runtime``. The capture path relies on that single binding.
"""

from __future__ import annotations


def test_snapshot_imports_from_the_library_package() -> None:
    """Verify `hephaestus.git.snapshot` imports and exposes its public helpers."""
    import hephaestus.git.snapshot as snapshot

    assert snapshot.__name__ == "hephaestus.git.snapshot"
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
    """Verify `hephaestus.git.snapshot` is a library module."""
    import hephaestus.git.snapshot as snapshot

    assert not snapshot.__name__.startswith("hephaestus.automation")
