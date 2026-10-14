"""Shared requirement for the real Just executable used by Fleet build tests."""

from __future__ import annotations

import shutil

import pytest

requires_just = pytest.mark.skipif(
    shutil.which("just") is None,
    reason="the real Just executable drives the fixed build recipe",
)
