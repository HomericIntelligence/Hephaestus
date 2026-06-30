"""Regression tests for removed deprecated retry APIs.

Guards issue #1420: ``retry_with_jitter`` was removed in favor of
``retry_with_backoff(jitter=True, max_delay=...)``. These tests prove the
symbol is no longer exposed from the module or subpackage surfaces, so it
cannot be accidentally re-introduced.
"""

from __future__ import annotations


def test_retry_with_jitter_removed_from_utils_surfaces() -> None:
    """``retry_with_jitter`` must be absent from module and subpackage surfaces."""
    import hephaestus.utils as utils_pkg
    import hephaestus.utils.retry as retry_mod

    assert not hasattr(retry_mod, "retry_with_jitter")
    assert not hasattr(utils_pkg, "retry_with_jitter")
    assert "retry_with_jitter" not in utils_pkg.__all__
