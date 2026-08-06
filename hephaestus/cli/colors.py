#!/usr/bin/env python3

"""ANSI color codes with automatic terminal accessibility controls.

Colors are enabled automatically only for TTY output unless an environment
override changes that policy. The precedence is the calling thread's explicit
``enable()`` or ``disable()``, non-empty ``NO_COLOR``, non-empty
``FORCE_COLOR`` or non-zero ``CLICOLOR_FORCE``, ``CLICOLOR=0``, and finally
stdout TTY detection. Explicit state is kept in ``threading.local()`` so one
thread cannot change another thread's override.
"""

import os
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


def _automatic_colors_enabled() -> bool:
    """Return whether the process environment permits terminal color."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True

    clicolor_force = os.environ.get("CLICOLOR_FORCE")
    if clicolor_force and clicolor_force != "0":
        return True
    if os.environ.get("CLICOLOR") == "0":
        return False
    return sys.stdout.isatty()


class _ColorsMeta(type):
    """Metaclass that computes color codes on access from the current policy."""

    def __getattr__(cls, name: str) -> str:
        if name in _CODES:
            override = getattr(_state, "enabled", None)
            enabled = _automatic_colors_enabled() if override is None else override
            return _CODES[name] if enabled else ""
        raise AttributeError(f"type object 'Colors' has no attribute {name!r}")


class Colors(metaclass=_ColorsMeta):
    """ANSI color codes governed by environment, TTY, and thread-local state.

    Colors follow the automatic policy on every access. Calling ``disable()``
    or ``enable()`` explicitly overrides that policy for the calling thread;
    ``auto()`` removes that override. Color codes are computed from an
    immutable mapping and never mutate the shared mapping.

    Usage::

        from hephaestus.cli.colors import Colors

        print(f"{Colors.OKGREEN}Success{Colors.ENDC}")
        Colors.disable()   # disables for current thread only
        Colors.enable()    # re-enables for current thread only
        Colors.auto()      # restores environment and TTY evaluation
    """

    @staticmethod
    def disable() -> None:
        """Disable colors for the calling thread."""
        _state.enabled = False

    @staticmethod
    def enable() -> None:
        """Enable colors for the calling thread."""
        _state.enabled = True

    @staticmethod
    def auto() -> None:
        """Restore automatic environment and TTY policy for this thread."""
        if hasattr(_state, "enabled"):
            del _state.enabled
