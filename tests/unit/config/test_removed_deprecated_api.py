"""Regression tests for removed deprecated config APIs.

Guards removed ambient-configuration APIs. These tests prove the symbols are
absent from module and subpackage surfaces so they cannot be re-introduced.
"""

from __future__ import annotations


def test_get_config_value_removed_from_config_surfaces() -> None:
    """``get_config_value`` must be absent from module and subpackage surfaces."""
    import hephaestus.config as config_pkg
    import hephaestus.config.utils as config_utils

    assert not hasattr(config_utils, "get_config_value")
    assert not hasattr(config_pkg, "get_config_value")
    assert "get_config_value" not in config_pkg.__all__


def test_merge_with_env_removed_from_config_surfaces() -> None:
    """The unbounded environment-to-config bridge must remain absent."""
    import hephaestus.config as config_pkg
    import hephaestus.config.utils as config_utils

    assert not hasattr(config_utils, "merge_with_env")
    assert not hasattr(config_pkg, "merge_with_env")
    assert "merge_with_env" not in config_pkg.__all__
