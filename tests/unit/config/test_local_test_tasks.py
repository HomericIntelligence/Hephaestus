"""Regression tests for self-contained local test tasks."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef, unused-ignore]

PIXI = Path(__file__).resolve().parents[3] / "pixi.toml"


def _tasks() -> dict[str, Any]:
    """Return the configured Pixi tasks."""
    data = tomllib.loads(PIXI.read_text(encoding="utf-8"))
    return data["tasks"]


def test_full_test_task_depends_on_editable_install() -> None:
    """The supported full test task installs console scripts before pytest."""
    assert _tasks()["test"] == {
        "cmd": "pytest",
        "depends-on": ["dev-install"],
    }
