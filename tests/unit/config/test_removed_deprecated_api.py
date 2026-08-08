"""Regression tests for removed deprecated config APIs.

Guards issue #1420: ``get_config_value`` was removed in favor of the explicit
pipeline ``load_config()`` → ``merge_with_env()`` → ``get_setting()``. These
tests prove the symbol is no longer exposed from the module or subpackage
surfaces, so it cannot be accidentally re-introduced.
"""

from __future__ import annotations


def test_get_config_value_removed_from_config_surfaces() -> None:
    """``get_config_value`` must be absent from module and subpackage surfaces."""
    import hephaestus.config as config_pkg
    import hephaestus.config.utils as config_utils

    assert not hasattr(config_utils, "get_config_value")
    assert not hasattr(config_pkg, "get_config_value")
    assert "get_config_value" not in config_pkg.__all__
