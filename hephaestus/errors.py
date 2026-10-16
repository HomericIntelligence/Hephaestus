"""Reproducible error reporting at module boundaries.

Every console script and every public library entry point reports a failure the
same way: one exit code, one stderr line, the same text for the same cause.
Call this at the boundary. Inside a helper, raise plainly and let the boundary
decide how to report.

Usage:
    from hephaestus.errors import boundary_main

    def main() -> int:
        ...

    if __name__ == "__main__":
        raise SystemExit(boundary_main(main))

"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, TextIO

__all__ = [
    "BoundaryError",
    "ConfigurationError",
    "ExitCode",
    "ExternalToolError",
    "InvalidInputError",
    "NotFoundError",
    "TimeoutExpiredError",
    "boundary_main",
    "format_error",
]


class ExitCode(IntEnum):
    """Process exit codes. The value is the contract; do not renumber."""

    OK = 0
    ERROR = 1
    INVALID_INPUT = 2
    NOT_FOUND = 3
    CONFIGURATION = 4
    EXTERNAL_TOOL = 5
    TIMEOUT = 6
    INTERRUPTED = 130


class BoundaryError(Exception):
    """A failure that a module boundary can report without inspecting a traceback.

    Attributes:
        message: What failed, in one line.
        exit_code: The process exit code for this failure class.
        recovery: What the operator or the next agent can do about it.

    """

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(
        self,
        message: str,
        *,
        recovery: str | None = None,
        exit_code: ExitCode | None = None,
    ) -> None:
        """Record one boundary failure.

        Args:
            message: What failed, in one line.
            recovery: The step that resolves it.
            exit_code: Overrides the class default when the cause is specific.

        """
        super().__init__(message)
        self.message = message
        self.recovery = recovery
        if exit_code is not None:
            self.exit_code = exit_code


class InvalidInputError(BoundaryError):
    """The caller supplied an argument this process cannot accept."""

    exit_code = ExitCode.INVALID_INPUT


class NotFoundError(BoundaryError):
    """A named file, directory, branch, or record does not exist."""

    exit_code = ExitCode.NOT_FOUND


class ConfigurationError(BoundaryError):
    """The environment or configuration file is missing a required value."""

    exit_code = ExitCode.CONFIGURATION


class ExternalToolError(BoundaryError):
    """A subprocess, `git`, or `gh` call failed."""

    exit_code = ExitCode.EXTERNAL_TOOL


class TimeoutExpiredError(BoundaryError):
    """An operation exceeded its deadline."""

    exit_code = ExitCode.TIMEOUT


@dataclass(frozen=True)
class ReportedError:
    """One rendered failure, ready to write to stderr.

    Attributes:
        exit_code: The process exit code.
        text: The full stderr block, newline-terminated.

    """

    exit_code: ExitCode
    text: str


def format_error(error: BaseException) -> ReportedError:
    """Render one exception as the boundary report for this process.

    The same cause always renders the same text, so two runs of the same
    failure are comparable.

    Args:
        error: The exception raised at the boundary.

    Returns:
        The exit code and stderr block for that exception.

    """
    if isinstance(error, BoundaryError):
        return ReportedError(
            error.exit_code,
            f"{error.__class__.__name__}: {error.message}\n"
            + (f"Recovery: {error.recovery}\n" if error.recovery else ""),
        )
    if isinstance(error, TimeoutError):
        return ReportedError(
            ExitCode.TIMEOUT,
            f"{error.__class__.__name__}: {error}\n"
            "Recovery: raise the timeout or check that the external host responds.\n",
        )
    if isinstance(error, KeyboardInterrupt):
        return ReportedError(ExitCode.INTERRUPTED, "Interrupted.\n")
    if isinstance(error, (FileNotFoundError, KeyError)):
        return ReportedError(
            ExitCode.NOT_FOUND,
            f"{error.__class__.__name__}: {error}\n",
        )
    if isinstance(error, (ValueError, TypeError, OSError)):
        return ReportedError(ExitCode.ERROR, f"{error.__class__.__name__}: {error}\n")
    return ReportedError(ExitCode.ERROR, f"{error.__class__.__name__}: {error}\n")


def boundary_main(
    entry: Callable[..., Any],
    /,
    *args: Any,
    stream: TextIO | None = None,
    **kwargs: Any,
) -> int:
    """Run a console-script entry point and report any failure reproducibly.

    Use this in place of a bare ``try``/``except Exception`` in a
    ``__main__`` block.

    Args:
        entry: The entry point to call.
        args: Positional arguments for ``entry``.
        stream: Where to write the report. Defaults to ``sys.stderr``.
        kwargs: Keyword arguments for ``entry``.

    Returns:
        The process exit code.

    """
    target = stream if stream is not None else sys.stderr
    try:
        result = entry(*args, **kwargs)
    except SystemExit as exit_request:
        # argparse and sys.exit raise SystemExit with the intended code. Pass
        # it through unchanged instead of folding it into a generic failure.
        code = exit_request.code
        if code is None:
            return int(ExitCode.OK)
        if isinstance(code, int):
            return code
        target.write(f"{code}\n")
        return int(ExitCode.ERROR)
    except KeyboardInterrupt:
        target.write("Interrupted.\n")
        return int(ExitCode.INTERRUPTED)
    except Exception as error:
        # A boundary reports the failure. It does not inspect the traceback and
        # it does not re-raise: the exit code is the contract.
        report = format_error(error)
        target.write(report.text)
        return int(report.exit_code)
    return result if isinstance(result, int) else int(ExitCode.OK)
