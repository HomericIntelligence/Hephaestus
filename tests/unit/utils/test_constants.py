#!/usr/bin/env python3
"""Tests for the shared constants module."""

import logging

import pytest

from hephaestus.constants import DEFAULT_EXCLUDE_DIRS, LOG_FORMAT


class TestDefaultExcludeDirs:
    """Tests for DEFAULT_EXCLUDE_DIRS."""

    def test_is_frozenset(self) -> None:
        """DEFAULT_EXCLUDE_DIRS must be a frozenset, not a mutable set."""
        assert isinstance(DEFAULT_EXCLUDE_DIRS, frozenset)

    @pytest.mark.parametrize(
        "directory",
        [
            ".git",
            "__pycache__",
            "node_modules",
            "venv",
            ".tox",
            ".pixi",
            ".pytest_cache",
            "dist",
            "build",
            ".mypy_cache",
            ".eggs",
        ],
    )
    def test_contains_expected_entry(self, directory: str) -> None:
        """Each expected directory is present in the frozenset."""
        assert directory in DEFAULT_EXCLUDE_DIRS

    def test_all_entries_are_strings(self) -> None:
        """Every entry in DEFAULT_EXCLUDE_DIRS is a string."""
        for entry in DEFAULT_EXCLUDE_DIRS:
            assert isinstance(entry, str)

    def test_immutability(self) -> None:
        """Frozenset should reject mutation attempts."""
        with pytest.raises(AttributeError):
            DEFAULT_EXCLUDE_DIRS.add("new_dir")  # type: ignore[attr-defined]

        with pytest.raises(AttributeError):
            DEFAULT_EXCLUDE_DIRS.discard(".git")  # type: ignore[attr-defined]


class TestLogFormat:
    """Tests for LOG_FORMAT."""

    def test_is_string(self) -> None:
        """LOG_FORMAT must be a string."""
        assert isinstance(LOG_FORMAT, str)

    def test_valid_logging_formatter(self) -> None:
        """LOG_FORMAT can be used to construct a logging.Formatter without error."""
        formatter = logging.Formatter(LOG_FORMAT)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello",
            args=None,
            exc_info=None,
        )
        # Should produce a non-empty formatted string
        formatted = formatter.format(record)
        assert len(formatted) > 0

    def test_contains_standard_fields(self) -> None:
        """LOG_FORMAT should reference standard logging fields."""
        assert "%(asctime)s" in LOG_FORMAT
        assert "%(name)s" in LOG_FORMAT
        assert "%(levelname)s" in LOG_FORMAT
        assert "%(message)s" in LOG_FORMAT
