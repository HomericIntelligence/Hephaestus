"""Tests for the shared PyYAML capability resolver."""

import sys

import pytest

from hephaestus.io.yaml import import_yaml


def test_returns_pyyaml_module() -> None:
    """The resolver returns PyYAML's module when the capability is installed."""
    module = import_yaml()
    assert module.__name__ == "yaml"
    assert hasattr(module, "safe_load")
    assert hasattr(module, "dump")


def test_missing_pyyaml_raises_actionable_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing PyYAML becomes one actionable, chained runtime error."""
    monkeypatch.setitem(sys.modules, "yaml", None)

    with pytest.raises(
        RuntimeError,
        match=r"Install with: pip install PyYAML",
    ) as exc_info:
        import_yaml()

    assert isinstance(exc_info.value.__cause__, ImportError)
