#!/usr/bin/env python3
"""Enhanced logging utilities for ProjectHephaestus.

Standardized logging interface with configurable output formats,
multiple destinations, and context management.

Usage:
    from hephaestus.logging.utils import get_logger, setup_logging

    setup_logging(level=logging.DEBUG)
    logger = get_logger(__name__)
    logger.info("This is an info message")
"""

import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any

from hephaestus.constants import LOG_FORMAT

# Module-level lock protects the check-then-add TOCTOU in get_logger()
_handler_setup_lock = threading.Lock()


class ContextLogger(logging.LoggerAdapter):  # type: ignore[type-arg]
    """Logger adapter that adds context information to log messages."""

    def __init__(self, logger: logging.Logger, context: dict[str, Any] | None = None) -> None:
        """Initialize with logger and optional context dict."""
        ctx = dict(context) if context else {}
        super().__init__(logger, ctx)
        self._context = ctx
        self._context_lock = threading.Lock()

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        """Add context information to log messages."""
        extra = kwargs.get("extra", {})
        with self._context_lock:
            extra.update(self._context)
        kwargs["extra"] = extra
        return msg, kwargs

    def bind(self, **kwargs: Any) -> "ContextLogger":
        """Create a new logger with additional context."""
        with self._context_lock:
            new_context = self._context.copy()
        new_context.update(kwargs)
        return ContextLogger(self.logger, new_context)

    def unbind(self, *keys: str) -> "ContextLogger":
        """Remove context keys from logger."""
        with self._context_lock:
            new_context = self._context.copy()
        for key in keys:
            new_context.pop(key, None)
        return ContextLogger(self.logger, new_context)


def get_logger(
    name: str,
    level: int | None = None,
    log_file: str | None = None,
    context: dict[str, Any] | None = None,
) -> ContextLogger:
    """Get a configured logger instance with optional context.

    Args:
        name: Logger name (typically __name__)
        level: Logging level (defaults to INFO)
        log_file: Optional file to log to
        context: Optional context dictionary to include in logs

    Returns:
        Configured ContextLogger instance

    """
    logger = logging.getLogger(name)
    logger.setLevel(level or logging.INFO)

    formatter = logging.Formatter(LOG_FORMAT)

    # Lock protects the check-then-add TOCTOU race condition during concurrent initialization
    with _handler_setup_lock:
        # Add console handler if one doesn't already exist
        has_console = any(
            isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
            for h in logger.handlers
        )
        if not has_console:
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        # Add file handler if requested and not already present for this path
        if log_file:
            resolved = str(Path(log_file).resolve())
            has_file = any(
                isinstance(h, logging.FileHandler) and h.baseFilename == resolved
                for h in logger.handlers
            )
            if not has_file:
                file_handler = logging.FileHandler(log_file)
                file_handler.setFormatter(formatter)
                logger.addHandler(file_handler)

    # Prevent duplicate output when root logger also has handlers
    # (e.g., from setup_logging() or logging.basicConfig()).
    # Safe to set outside the lock — simple attribute assignment on the logger object.
    logger.propagate = False

    return ContextLogger(logger, context)


def setup_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
    format_string: str | None = None,
    log_to_stderr: bool = False,
) -> None:
    """Set up global logging configuration.

    Args:
        level: Default logging level
        log_file: Optional file to log to
        format_string: Custom log format
        log_to_stderr: Whether to also log to stderr

    """
    format_string = format_string or LOG_FORMAT
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    formatter = logging.Formatter(format_string)

    # Deduplicate stdout StreamHandler
    has_stdout = any(
        isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
        and getattr(h, "stream", None) is sys.stdout
        for h in root_logger.handlers
    )
    if not has_stdout:
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)

    # Deduplicate stderr StreamHandler
    if log_to_stderr:
        has_stderr = any(
            isinstance(h, logging.StreamHandler)
            and not isinstance(h, logging.FileHandler)
            and getattr(h, "stream", None) is sys.stderr
            for h in root_logger.handlers
        )
        if not has_stderr:
            stderr_handler = logging.StreamHandler(sys.stderr)
            stderr_handler.setFormatter(formatter)
            root_logger.addHandler(stderr_handler)

    # Deduplicate FileHandler by resolved path
    if log_file:
        abs_log_file = os.path.abspath(log_file)
        has_file = any(
            isinstance(h, logging.FileHandler) and h.baseFilename == abs_log_file
            for h in root_logger.handlers
        )
        if not has_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            root_logger.addHandler(file_handler)
