"""Tests for safe host-verification path handling."""

from __future__ import annotations

import pytest

from hephaestus.git.verification.paths import safe_changed_paths, safe_relative_path


def test_safe_relative_path_keeps_a_normal_repository_path() -> None:
    """A canonical relative path is returned unchanged."""
    assert safe_relative_path("tests/unit/git/verification/test_paths.py") == (
        "tests/unit/git/verification/test_paths.py"
    )


@pytest.mark.parametrize(
    "value",
    ["/absolute/file.py", "../outside.py", "a/../b.py", "./file.py", "", "bad\x00path", 17],
)
def test_safe_relative_path_rejects_unsafe_values(value: object) -> None:
    """Absolute, escaping, empty, and non-string values are refused."""
    assert safe_relative_path(value) is None


def test_safe_changed_paths_enforces_the_count_bound() -> None:
    """More paths than the caller allows produce no result."""
    assert safe_changed_paths(["a.py", "b.py"], count_max=2, bytes_max=64) == ("a.py", "b.py")
    assert safe_changed_paths(["a.py", "b.py"], count_max=1, bytes_max=64) is None


def test_safe_changed_paths_enforces_the_byte_bound_and_uniqueness() -> None:
    """Oversized and duplicate path sets are refused."""
    assert safe_changed_paths(["abcd.py"], count_max=4, bytes_max=5) is None
    assert safe_changed_paths(["a.py", "a.py"], count_max=4, bytes_max=64) is None
    assert safe_changed_paths(["../a.py"], count_max=4, bytes_max=64) is None
