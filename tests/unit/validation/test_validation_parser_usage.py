"""Behavioral checks for packaged validation CLI entry points."""

from __future__ import annotations

import importlib
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


def _validation_entry_points() -> list[tuple[str, str]]:
    """Discover validation console scripts from the project metadata."""
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return sorted(
        (command, target)
        for command, target in project["project"]["scripts"].items()
        if target.startswith("hephaestus.validation.")
    )


def _load_entry_point(target: str) -> Callable[..., int]:
    """Load a ``module:function`` console-script target."""
    module_name, function_name = target.split(":", 1)
    return getattr(importlib.import_module(module_name), function_name)


def test_validation_entry_points_are_discovered() -> None:
    """The metadata-driven discovery contract must not pass vacuously."""
    assert _validation_entry_points()


@pytest.mark.parametrize(
    ("command", "target"),
    _validation_entry_points(),
)
def test_validation_entry_points_expose_shared_version_behavior(
    command: str,
    target: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every discovered validation entry point accepts ``--version``."""
    monkeypatch.setattr(sys, "argv", [command, "--version"])

    with pytest.raises(SystemExit) as exited:
        _load_entry_point(target)()

    assert exited.value.code == 0
    assert capsys.readouterr().out.strip()
