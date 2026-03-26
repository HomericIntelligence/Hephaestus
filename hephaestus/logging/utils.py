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
import sys
import threading
from typing import Any

from hephaestus.constants import LOG_FORMAT


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

    # Prevent adding handlers multiple times
    if not logger.handlers:
        formatter = logging.Formatter(LOG_FORMAT)

        # Console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # File handler (optional)
        if log_file:
            file_handler = logging.FileHandler(log_file)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

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

    handlers = [logging.StreamHandler(sys.stdout)]

    if log_to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))

    logging.basicConfig(level=level, format=format_string, handlers=handlers)

    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(format_string))
        logging.getLogger().addHandler(file_handler)
