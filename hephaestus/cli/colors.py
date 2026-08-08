#!/usr/bin/env python3

"""ANSI color codes for terminal output.

Provides a thread-safe Colors class with standard ANSI codes and utilities
for disabling colors in non-terminal environments. Each thread maintains
its own enabled/disabled state via ``threading.local()``.
"""

import sys
import threading

# Thread-local storage for per-thread color enabled state
_state = threading.local()

# Immutable mapping of color names to ANSI codes
_CODES: dict[str, str] = {
    "HEADER": "\033[95m",
    "OKBLUE": "\033[94m",
    "OKCYAN": "\033[96m",
    "OKGREEN": "\033[92m",
    "WARNING": "\033[93m",
    "FAIL": "\033[91m",
    "ENDC": "\033[0m",
    "BOLD": "\033[1m",
    "UNDERLINE": "\033[4m",
}


class _ColorsMeta(type):
    """Metaclass that computes color codes on access from thread-local state."""

    def __getattr__(cls, name: str) -> str:
        if name in _CODES:
            enabled = getattr(_state, "enabled", True)
            return _CODES[name] if enabled else ""
        raise AttributeError(f"type object 'Colors' has no attribute {name!r}")


class Colors(metaclass=_ColorsMeta):
    """ANSI color codes for terminal output.

    Thread-safe: each thread maintains its own enabled/disabled state.
    Calling ``disable()`` or ``enable()`` only affects the calling thread.
    Color codes are computed on access from an immutable mapping, never mutated.

    Usage::

        from hephaestus.cli.colors import Colors

        print(f"{Colors.OKGREEN}Success{Colors.ENDC}")
        Colors.disable()   # disables for current thread only
        Colors.enable()    # re-enables for current thread only
        Colors.auto()      # disables if stdout is not a TTY
    """

    @staticmethod
    def disable() -> None:
        """Disable colors for the current thread.

        Sets the per-thread enabled flag to ``False`` so all color code
        lookups return empty strings for this thread only.
        """
        _state.enabled = False

    @staticmethod
    def enable() -> None:
        """Enable colors for the current thread.

        Sets the per-thread enabled flag to ``True`` so all color code
        lookups return ANSI escape sequences for this thread only.
        """
        _state.enabled = True

    @staticmethod
    def auto() -> None:
        """Automatically disable colors if stdout is not a TTY.

        Call this at the start of a script to automatically handle
        color output based on the environment. Only affects the
        calling thread.
        """
        if not sys.stdout.isatty():
            Colors.disable()
