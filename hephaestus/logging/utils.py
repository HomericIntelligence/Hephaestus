#!/usr/bin/env python3
"""
Enhanced logging utilities for ProjectHephaestus.

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
from typing import Optional, Dict, Any
from pathlib import Path
from contextlib import contextmanager


class ContextLogger(logging.LoggerAdapter):
    """Logger adapter that adds context information to log messages."""
    
    def __init__(self, logger: logging.Logger, context: Optional[Dict[str, Any]] = None):
        super().__init__(logger, context or {})
        self._context = context or {}
        self._context_lock = threading.Lock()
    
    def process(self, msg, kwargs):
        """Add context information to log messages."""
        extra = kwargs.get('extra', {})
        extra.update(self._context)
        kwargs['extra'] = extra
        return msg, kwargs
    
    def bind(self, **kwargs):
        """Create a new logger with additional context."""
        new_context = self._context.copy()
        new_context.update(kwargs)
        return ContextLogger(self.logger, new_context)
    
    def unbind(self, *keys):
        """Remove context keys from logger."""
        new_context = self._context.copy()
        for key in keys:
            new_context.pop(key, None)
        return ContextLogger(self.logger, new_context)


def get_logger(name: str, 
               level: Optional[int] = None,
               log_file: Optional[str] = None,
               context: Optional[Dict[str, Any]] = None) -> ContextLogger:
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
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
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


def setup_logging(level: int = logging.INFO,
                  log_file: Optional[str] = None,
                  format_string: Optional[str] = None,
                  log_to_stderr: bool = False) -> None:
    """Setup global logging configuration.
    
    Args:
        level: Default logging level
        log_file: Optional file to log to
        format_string: Custom log format
        log_to_stderr: Whether to also log to stderr
    """
    format_string = format_string or '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    handlers = [logging.StreamHandler(sys.stdout)]
    
    if log_to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    
    logging.basicConfig(
        level=level,
        format=format_string,
        handlers=handlers
    )
    
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(format_string))
        logging.getLogger().addHandler(file_handler)


@contextmanager
def log_context(**context):
    """Context manager for temporary logging context.
    
    Usage:
        with log_context(user_id="12345", request_id="abc"):
            logger.info("Processing request")
    """
    # This would normally integrate with a global context system
    # For now, we'll just yield and cleanup
    try:
        yield
    finally:
        pass  # Cleanup would happen here in a real implementation


def create_rotating_file_logger(name: str, 
                               log_file: str,
                               max_bytes: int = 10485760,  # 10MB
                               backup_count: int = 5) -> ContextLogger:
    """Create a logger with rotating file handler.
    
    Args:
        name: Logger name
        log_file: Log file path
        max_bytes: Maximum file size before rotation
        backup_count: Number of backup files to keep
        
    Returns:
        Configured ContextLogger with rotating file handler
    """
    try:
        from logging.handlers import RotatingFileHandler
    except ImportError:
        # Fallback to regular file handler
        return get_logger(name, log_file=log_file)
    
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    
    # Create rotating file handler
    handler = RotatingFileHandler(
        log_file, 
        maxBytes=max_bytes, 
        backupCount=backup_count
    )
    
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return ContextLogger(logger)
