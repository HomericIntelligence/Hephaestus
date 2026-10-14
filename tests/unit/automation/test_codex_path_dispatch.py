"""Check path guard dispatch without access to protected resources."""

from __future__ import annotations

import ast
import importlib.util
import os
import signal
import subprocess
import threading
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation.codex_adapter_admission import _ISOLATED_ADAPTER_HELPER


@pytest.fixture
def path_dispatch(tmp_path: Path) -> tuple[Any, Mock]:
    """Load only the capability wrapper with controlled boundary functions."""
    helper = ast.parse(_ISOLATED_ADAPTER_HELPER)
    wrapper = next(
        node
        for node in helper.body
        if isinstance(node, ast.ClassDef) and node.name == "CapabilityObject"
    )
    guard = Mock(return_value="guarded")
    namespace: dict[str, Any] = {
        "capability_targets": {},
        "threading": threading,
        "signal": signal,
        "os": os,
        "subprocess": subprocess,
        "descriptor_first_argument": set(),
        "path_api_targets": set(),
        "call_path_api": guard,
        "close_value": lambda value: value,
        "close_callback_payload": lambda value: value,
    }
    source = tmp_path / "capability_dispatch.py"
    source.write_text(ast.unparse(wrapper), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("capability_dispatch_test", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    vars(module).update(namespace)
    spec.loader.exec_module(module)
    return module.CapabilityObject, guard


@pytest.mark.parametrize("module_name", ["pathlib", "pathlib._local", "shutil", "shutil._local"])
def test_path_module_calls_use_the_guard(path_dispatch: tuple[Any, Mock], module_name: str) -> None:
    """The root module and its submodules must use the same path guard."""
    wrapper, guard = path_dispatch
    target = Mock(return_value="direct")
    target.__module__ = module_name

    result = wrapper(target)("ordinary.txt", encoding="utf-8")

    assert result == "guarded"
    guard.assert_called_once_with(target, ("ordinary.txt",), {"encoding": "utf-8"})
    target.assert_not_called()


@pytest.mark.parametrize("module_name", ["pathlib_extra", "shutil_extra", "example.pathlib"])
def test_other_modules_keep_their_call_route(
    path_dispatch: tuple[Any, Mock], module_name: str
) -> None:
    """A similar module name must not select the path guard."""
    wrapper, guard = path_dispatch
    target = Mock(return_value="direct")
    target.__module__ = module_name

    assert wrapper(target)("ordinary.txt") == "direct"

    guard.assert_not_called()
    target.assert_called_once_with("ordinary.txt")


def test_bound_path_method_keeps_ordinary_file_access(
    path_dispatch: tuple[Any, Mock], tmp_path: Path
) -> None:
    """A guarded bound method can write an ordinary temporary file."""
    wrapper, guard = path_dispatch
    path = tmp_path / "ordinary.txt"
    target = path.write_text
    guard.side_effect = lambda function, args, kwargs: function(*args, **kwargs)

    assert wrapper(target)("content", encoding="utf-8") == 7

    guard.assert_called_once_with(target, ("content",), {"encoding": "utf-8"})
    assert path.read_text(encoding="utf-8") == "content"
