#!/usr/bin/env python3
"""Tests for the shared constants module."""

import importlib
import logging
import os

import pytest

from hephaestus.constants import (
    AUTOMATION_LOG_FORMAT,
    DEFAULT_EXCLUDE_DIRS,
    LOG_DATEFMT,
    LOG_FORMAT,
)
from hephaestus.utils import helpers


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
            ".venv",
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


class TestAutomationLogFormat:
    """Tests for AUTOMATION_LOG_FORMAT / LOG_DATEFMT (issue #1427)."""

    def test_is_string(self) -> None:
        """AUTOMATION_LOG_FORMAT and LOG_DATEFMT must be strings."""
        assert isinstance(AUTOMATION_LOG_FORMAT, str)
        assert isinstance(LOG_DATEFMT, str)

    def test_valid_logging_formatter(self) -> None:
        """AUTOMATION_LOG_FORMAT must build a usable logging.Formatter."""
        formatter = logging.Formatter(AUTOMATION_LOG_FORMAT, datefmt=LOG_DATEFMT)
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="hello",
            args=None,
            exc_info=None,
        )
        formatted = formatter.format(record)
        assert len(formatted) > 0

    def test_uses_bracketed_level_layout(self) -> None:
        """AUTOMATION_LOG_FORMAT uses the readable bracketed CLI layout."""
        assert "[%(levelname)s]" in AUTOMATION_LOG_FORMAT
        assert "%(name)s:" in AUTOMATION_LOG_FORMAT

    def test_distinct_from_library_format(self) -> None:
        """The automation format is intentionally distinct from LOG_FORMAT."""
        assert AUTOMATION_LOG_FORMAT != LOG_FORMAT


class TestSubprocessTimeouts:
    """Tests for subprocess timeout constants."""

    def test_metadata_timeout_exists(self) -> None:
        """METADATA_TIMEOUT constant is defined."""
        assert hasattr(helpers, "METADATA_TIMEOUT")
        assert isinstance(helpers.METADATA_TIMEOUT, int)
        assert helpers.METADATA_TIMEOUT > 0

    def test_network_timeout_exists(self) -> None:
        """NETWORK_TIMEOUT constant is defined."""
        assert hasattr(helpers, "NETWORK_TIMEOUT")
        assert isinstance(helpers.NETWORK_TIMEOUT, int)
        assert helpers.NETWORK_TIMEOUT > 0

    def test_network_timeout_longer_than_metadata(self) -> None:
        """NETWORK_TIMEOUT should be longer than METADATA_TIMEOUT."""
        assert helpers.NETWORK_TIMEOUT >= helpers.METADATA_TIMEOUT

    def test_default_metadata_timeout_is_10(self) -> None:
        """Default METADATA_TIMEOUT is 10 seconds."""
        if "HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT" not in os.environ:
            assert helpers.METADATA_TIMEOUT == 10

    def test_default_network_timeout_is_120(self) -> None:
        """Default NETWORK_TIMEOUT is 120 seconds."""
        if "HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT" not in os.environ:
            assert helpers.NETWORK_TIMEOUT == 120

    def test_metadata_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """METADATA_TIMEOUT respects HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT env var."""
        monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_METADATA_TIMEOUT", "5")
        # Reimport to pick up the new env var
        importlib.reload(helpers)
        try:
            assert helpers.METADATA_TIMEOUT == 5
        finally:
            # Restore original values
            importlib.reload(helpers)

    def test_network_timeout_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """NETWORK_TIMEOUT respects HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT env var."""
        monkeypatch.setenv("HEPHAESTUS_SUBPROCESS_NETWORK_TIMEOUT", "300")
        # Reimport to pick up the new env var
        importlib.reload(helpers)
        try:
            assert helpers.NETWORK_TIMEOUT == 300
        finally:
            # Restore original values
            importlib.reload(helpers)
