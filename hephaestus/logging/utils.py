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

import contextvars
import logging
import os
import sys
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal

from hephaestus.constants import LOG_FORMAT
from hephaestus.logging.formatters import JsonFormatter

# Module-level lock protects the check-then-add TOCTOU in get_logger()
_handler_setup_lock = threading.Lock()

# Honour HEPHAESTUS_LOG_FORMAT=json so logging format can be configured at
# deployment time without code changes (12-factor pattern).
_ENV_JSON_FORMAT: bool = os.environ.get("HEPHAESTUS_LOG_FORMAT", "").lower() == "json"

# Context variable for correlation ID propagation to subprocesses (thread- and async-safe)
_correlation_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


# WHY justified: logging.LoggerAdapter is non-generic at runtime on Python 3.10
# (PEP 585-style subscription was added in 3.11); parameterizing it would break
# at import time, so we suppress disallow_any_generics' [type-arg] here.
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
        with self._context_lock:
            # Create a new dict to avoid mutating the caller's extra dict across calls.
            kwargs["extra"] = {**kwargs.get("extra", {}), **self._context}
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

    def with_correlation_id(self, correlation_id: str | None = None) -> "ContextLogger":
        """Return a new logger with a correlation_id context key.

        Args:
            correlation_id: Explicit correlation ID string. If omitted, a
                random UUID4 is generated.

        Returns:
            New ContextLogger with ``correlation_id`` bound to context.

        """
        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        return self.bind(correlation_id=cid)


def set_correlation_id(correlation_id: str) -> contextvars.Token[str | None]:
    """Set the ambient correlation ID for subprocess propagation.

    Thread- and async-safe via contextvars. Intended for callers who manage
    their own cleanup via _correlation_id_var.reset(token). Most callers
    should use the correlation_id_scope() context manager instead.

    Args:
        correlation_id: Correlation ID string to set.

    Returns:
        A Token that can be used to restore the previous value.

    """
    return _correlation_id_var.set(correlation_id)


def get_current_correlation_id() -> str | None:
    """Get the current ambient correlation ID, if set.

    Thread- and async-safe via contextvars. Returns None if no correlation
    ID is currently bound.

    Returns:
        The current correlation ID string, or None.

    """
    return _correlation_id_var.get()


@contextmanager
def correlation_id_scope(correlation_id: str) -> Generator[None, None, None]:
    """Context manager to set an ambient correlation ID for subprocess propagation.

    The correlation ID is set on entry and restored on exit (thread- and
    async-safe via contextvars). This is the primary API for subprocess
    correlation ID propagation.

    Args:
        correlation_id: Correlation ID string to set for the scope.

    Yields:
        None.

    Example:
        with correlation_id_scope("request-abc-123"):
            _gh_call(["issue", "create"])  # gh process sees GH_TRACE_ID=request-abc-123

    """
    token = set_correlation_id(correlation_id)
    try:
        yield
    finally:
        _correlation_id_var.reset(token)


def get_logger(
    name: str,
    level: int | None = None,
    log_file: str | None = None,
    context: dict[str, Any] | None = None,
    json_format: bool = False,
    propagate: bool = False,
) -> ContextLogger:
    """Get a configured logger instance with optional context.

    Args:
        name: Logger name (typically __name__)
        level: Logging level (defaults to INFO)
        log_file: Optional file to log to
        context: Optional context dictionary to include in logs
        json_format: If True, use structured JSON output instead of plain text
        propagate: If True, allow log records to propagate to the root logger.
            Defaults to False to prevent duplicate output when a root logger
            is also configured (e.g. via setup_logging() or basicConfig()).

    Returns:
        Configured ContextLogger instance

    """
    logger = logging.getLogger(name)
    logger.setLevel(level or logging.INFO)

    use_json = json_format or _ENV_JSON_FORMAT
    formatter: logging.Formatter = JsonFormatter() if use_json else logging.Formatter(LOG_FORMAT)

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

    # Prevent duplicate output when root logger also has handlers.
    # Safe to set outside the lock — simple attribute assignment on the logger object.
    logger.propagate = propagate

    return ContextLogger(logger, context)


def setup_logging(
    level: int = logging.INFO,
    log_file: str | None = None,
    format_string: str | None = None,
    log_to_stderr: bool = False,
    json_format: bool = False,
    datefmt: str | None = None,
    primary_stream: Literal["stdout", "stderr"] = "stdout",
) -> None:
    """Set up global logging configuration.

    Args:
        level: Default logging level
        log_file: Optional file to log to
        format_string: Custom log format (ignored when *json_format* is True)
        log_to_stderr: Whether to also log to stderr
        json_format: If True, use structured JSON output instead of plain text
        datefmt: Optional date format for text log records
        primary_stream: Console stream for the primary root handler

    """
    if primary_stream not in {"stdout", "stderr"}:
        raise ValueError("primary_stream must be 'stdout' or 'stderr'")

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter: logging.Formatter
    if json_format or _ENV_JSON_FORMAT:
        formatter = JsonFormatter()
    else:
        format_string = format_string or LOG_FORMAT
        formatter = logging.Formatter(format_string, datefmt=datefmt)

    primary_target = sys.stderr if primary_stream == "stderr" else sys.stdout
    stream_targets = [primary_target]
    if log_to_stderr and sys.stderr not in stream_targets:
        stream_targets.append(sys.stderr)

    # Lock protects the check-then-add TOCTOU race on the root logger's handler list
    with _handler_setup_lock:
        # Deduplicate console StreamHandlers by stream identity.
        for stream in stream_targets:
            has_stream = any(
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.FileHandler)
                and getattr(h, "stream", None) is stream
                for h in root_logger.handlers
            )
            if not has_stream:
                stream_handler = logging.StreamHandler(stream)
                stream_handler.setFormatter(formatter)
                root_logger.addHandler(stream_handler)

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
