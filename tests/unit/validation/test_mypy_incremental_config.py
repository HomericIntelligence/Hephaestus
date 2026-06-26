"""Guard: [tool.mypy] must declare incremental mode explicitly (issue #1503).

The mypy pre-commit hook runs the full codebase on every .py change
(pass_filenames: false). Incremental mode is mypy's default but was previously
unconfigured; this test makes the invariant executable so a future edit cannot
silently disable it.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parents[3] / "pyproject.toml"


@pytest.fixture(scope="module")
def mypy_config() -> dict[str, object]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["mypy"]


def test_incremental_enabled(mypy_config: dict[str, object]) -> None:
    assert mypy_config.get("incremental") is True


def test_cache_dir_pinned(mypy_config: dict[str, object]) -> None:
    assert mypy_config.get("cache_dir") == ".mypy_cache"
