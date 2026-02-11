#!/usr/bin/env python3
"""
Logging utilities for ProjectHephaestus.

Standardized logging interface with configurable output formats
and multiple destinations.

Usage:
    from hephaestus.logging.utils import get_logger
    
    logger = get_logger(__name__)
    logger.info("This is an info message")
"""

import logging
import sys
from typing import Optional
from pathlib import Path

def get_logger(name: str, 
               level: Optional[int] = None,
               log_file: Optional[str] = None) -> logging.Logger:
    """Get a configured logger instance.
    
    Args:
        name: Logger name (typically __name__)
        level: Logging level (defaults to INFO)
        log_file: Optional file to log to
        
    Returns:
        Configured Logger instance
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
    
    return logger

def setup_logging(level: int = logging.INFO,
                  log_file: Optional[str] = None,
                  format_string: Optional[str] = None) -> None:
    """Setup global logging configuration.
    
    Args:
        level: Default logging level
        log_file: Optional file to log to
        format_string: Custom log format
    """
    format_string = format_string or '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    logging.basicConfig(
        level=level,
        format=format_string,
        handlers=[
            logging.StreamHandler(sys.stdout),
        ]
    )
    
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(logging.Formatter(format_string))
        logging.getLogger().addHandler(file_handler)
