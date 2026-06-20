"""Unit tests for ``hephaestus.automation.prompts._shared._relativize_path``.

Covers the repo-relative happy path and the two benign absolute-path
fallbacks, which are logged at DEBUG (visible only under -v/--verbose) rather
than WARNING so they do not add routine noise to default runs (#1556).
"""

from __future__ import annotations

import logging

import pytest

from hephaestus.automation.prompts import _shared

_LOGGER_NAME = _shared._prompts_logger.name


def test_path_under_repo_root_is_relativized(tmp_path) -> None:
    """A path inside repo_root is returned relative, with no log output."""
    repo_root = tmp_path
    target = tmp_path / "worktrees" / "123-fix"
    result = _shared._relativize_path(str(target), str(repo_root))
    assert result == "worktrees/123-fix"


def test_path_outside_repo_root_logs_debug_not_warning(
    tmp_path, caplog: pytest.LogCaptureFixture
) -> None:
    """A cross-repo path keeps the absolute path and logs at DEBUG only (#1556)."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "other" / "marketplace.json"

    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result_info = _shared._relativize_path(str(outside), str(repo_root))
    assert result_info == str(outside)
    # Nothing at INFO or above — the benign fallback is quiet on default runs.
    assert caplog.records == []

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        result_debug = _shared._relativize_path(str(outside), str(repo_root))
    assert result_debug == str(outside)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "is not under repo_root" in caplog.records[0].getMessage()


def test_no_repo_root_logs_debug_not_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """repo_root=None returns the path unchanged and logs at DEBUG only (#1556)."""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        result_info = _shared._relativize_path("/abs/path", None)
    assert result_info == "/abs/path"
    assert caplog.records == []

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger=_LOGGER_NAME):
        result_debug = _shared._relativize_path("/abs/path", None)
    assert result_debug == "/abs/path"
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.DEBUG
    assert "repo_root not provided" in caplog.records[0].getMessage()


def test_empty_path_returned_unchanged() -> None:
    """An empty path short-circuits with no logging."""
    assert _shared._relativize_path("", "/repo") == ""
