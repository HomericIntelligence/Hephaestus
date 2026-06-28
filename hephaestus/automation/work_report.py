"""Work report file protocol for loop runner (#613).

The loop runner injects HEPH_WORK_REPORT (a temp file path) into subprocess
envs. Phases that understand the contract write their work-unit count to that
file. The runner reads the file after the subprocess returns to measure
convergence.

No-op when the env var is unset (phase run outside the loop runner).
"""

import contextlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path


def write_work_report(work_units: int) -> None:
    """Write the phase's work-unit count to the path in $HEPH_WORK_REPORT.

    Args:
        work_units: The number of work units (e.g., issues planned or reviewed).

    Note:
        No-op when the env var is unset (phase run outside the loop runner).

    """
    path = os.environ.get("HEPH_WORK_REPORT")
    if not path:
        return
    # best-effort; absence ⇒ "unknown" ⇒ treated as work
    with contextlib.suppress(OSError):
        Path(path).write_text(str(int(work_units)), encoding="utf-8")


@contextlib.contextmanager
def work_report_context(work_units_fn: Callable[[], int]) -> Iterator[None]:
    """Write a work report when the loop runner requested one.

    The report env var remains optional so phases still run outside the loop
    runner. When it is present on entry, the work-unit callback is evaluated on
    exit and written through write_work_report().

    Args:
        work_units_fn: Callback returning the work-unit count to report.

    """
    if not os.environ.get("HEPH_WORK_REPORT"):
        yield
        return

    try:
        yield
    finally:
        # Best-effort reporting: suppress reporting failures without masking the block's exception.
        with contextlib.suppress(Exception):
            write_work_report(work_units_fn())
