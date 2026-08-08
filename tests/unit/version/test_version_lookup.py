"""Unit tests for hephaestus._version_lookup."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from unittest.mock import patch

from hephaestus._version_lookup import get_version


def test_get_version_returns_installed_version() -> None:
    """When the distribution is installed, get_version returns its metadata value."""
    with patch("hephaestus._version_lookup._pkg_version", return_value="1.2.3"):
        assert get_version() == "1.2.3"


def test_get_version_falls_back_to_unknown() -> None:
    """When the distribution is not installed, get_version returns 'unknown'."""
    with patch(
        "hephaestus._version_lookup._pkg_version",
        side_effect=PackageNotFoundError("HomericIntelligence-Hephaestus"),
    ):
        assert get_version() == "unknown"


def test_get_version_returns_str() -> None:
    """Resolved value is always a string (live call against installed dist)."""
    result = get_version()
    assert isinstance(result, str)
    assert result  # non-empty
