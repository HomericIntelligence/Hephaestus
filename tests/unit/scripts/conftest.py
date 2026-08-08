"""Shared fixtures for scripts/ smoke tests.

Auto-discovers every ``scripts/*.py`` file and exposes it as a parametrized
fixture so a single ``--help`` smoke test guards every script. New scripts
get tested automatically — no per-script wiring required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _discover_scripts() -> list[Path]:
    """Return all top-level ``scripts/*.py`` files (excluding dunder files)."""
    return sorted(p for p in SCRIPTS_DIR.glob("*.py") if not p.name.startswith("_"))


@pytest.fixture(params=_discover_scripts(), ids=lambda p: p.name)
def script_path(request: pytest.FixtureRequest) -> Path:
    """One ``scripts/*.py`` file per parametrization."""
    return request.param
