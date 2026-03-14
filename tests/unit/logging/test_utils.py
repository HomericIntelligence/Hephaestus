#!/usr/bin/env python3
"""Tests for logging utilities."""

import logging
from pathlib import Path

from hephaestus.logging.utils import (
    ContextLogger,
    get_logger,
    setup_logging,
)


class TestGetLogger:
    """Tests for get_logger function."""

    def test_returns_context_logger(self) -> None:
        """get_logger returns a ContextLogger instance."""
        logger = get_logger("test.module")
        assert isinstance(logger, ContextLogger)

    def test_default_level_is_info(self) -> None:
        """Logger defaults to INFO level."""
        logger = get_logger("test.default_level")
        assert logger.logger.level == logging.INFO

    def test_custom_level(self) -> None:
        """Logger respects custom level argument."""
        logger = get_logger("test.custom_level", level=logging.DEBUG)
        assert logger.logger.level == logging.DEBUG

    def test_with_context(self) -> None:
        """Logger stores supplied context."""
        ctx = {"request_id": "abc123"}
        logger = get_logger("test.context", context=ctx)
        assert logger._context == ctx

    def test_with_log_file(self, tmp_path: Path) -> None:
        """Logger creates file handler when log_file is given."""
        log_file = str(tmp_path / "test.log")
        logger = get_logger("test.file_handler", log_file=log_file)
        handler_types = [type(h) for h in logger.logger.handlers]
        assert logging.FileHandler in handler_types


class TestContextLogger:
    """Tests for ContextLogger adapter."""

    def test_bind_adds_context(self) -> None:
        """bind() returns new logger with merged context."""
        base = get_logger("test.bind", context={"a": 1})
        bound = base.bind(b=2)
        assert bound._context == {"a": 1, "b": 2}

    def test_bind_does_not_mutate_original(self) -> None:
        """bind() does not modify the original logger's context."""
        base = get_logger("test.bind_immutable", context={"a": 1})
        base.bind(b=2)
        assert "b" not in base._context

    def test_unbind_removes_key(self) -> None:
        """unbind() returns logger without the specified key."""
        base = get_logger("test.unbind", context={"a": 1, "b": 2})
        unbound = base.unbind("a")
        assert "a" not in unbound._context
        assert unbound._context["b"] == 2

    def test_process_adds_extra(self) -> None:
        """process() merges context into kwargs['extra']."""
        logger = get_logger("test.process", context={"x": 42})
        _msg, kwargs = logger.process("hello", {})
        assert kwargs["extra"]["x"] == 42


class TestSetupLogging:
    """Tests for setup_logging function."""

    def test_runs_without_error(self) -> None:
        """setup_logging runs without raising."""
        setup_logging(level=logging.WARNING)

    def test_with_log_file(self, tmp_path: Path) -> None:
        """setup_logging creates log file handler."""
        log_file = str(tmp_path / "setup.log")
        setup_logging(log_file=log_file)
        root_logger = logging.getLogger()
        handler_files = [getattr(h, "baseFilename", None) for h in root_logger.handlers]
        assert log_file in handler_files

    def test_log_to_stderr(self) -> None:
        """setup_logging with log_to_stderr=True adds a stderr StreamHandler."""
        import sys

        root = logging.getLogger()
        # basicConfig is a no-op if handlers already exist; clear them first.
        saved = list(root.handlers)
        root.handlers.clear()
        try:
            setup_logging(log_to_stderr=True)
            stderr_handlers = [
                h
                for h in root.handlers
                if isinstance(h, logging.StreamHandler) and h.stream is sys.stderr
            ]
            assert len(stderr_handlers) >= 1
        finally:
            root.handlers.clear()
            root.handlers.extend(saved)
