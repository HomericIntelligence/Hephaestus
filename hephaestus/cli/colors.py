#!/usr/bin/env python3

"""ANSI color codes for terminal output.

Provides a simple Colors class with standard ANSI codes and utilities
for disabling colors in non-terminal environments.
"""

import sys


class Colors:
    """ANSI color codes for terminal output.

    Note: This class uses class-level mutable state (color code strings).
    The ``disable()`` and ``auto()`` methods mutate class attributes and are
    therefore **not thread-safe**.  Call them once at program startup before
    spawning threads; do not call them concurrently.
    """

    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"

    @staticmethod
    def disable() -> None:
        """Disable colors for non-terminal output.

        Sets all color codes to empty strings, useful for piping
        output to files or non-TTY streams.
        """
        Colors.HEADER = ""
        Colors.OKBLUE = ""
        Colors.OKCYAN = ""
        Colors.OKGREEN = ""
        Colors.WARNING = ""
        Colors.FAIL = ""
        Colors.ENDC = ""
        Colors.BOLD = ""
        Colors.UNDERLINE = ""

    @staticmethod
    def auto() -> None:
        """Automatically disable colors if stdout is not a TTY.

        Call this at the start of a script to automatically handle
        color output based on the environment.
        """
        if not sys.stdout.isatty():
            Colors.disable()
