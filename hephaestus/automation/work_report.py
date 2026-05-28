"""Work report file protocol for loop runner (#613).

The loop runner injects HEPH_WORK_REPORT (a temp file path) into subprocess
envs. Phases that understand the contract write their work-unit count to that
file. The runner reads the file after the subprocess returns to measure
convergence.

No-op when the env var is unset (phase run outside the loop runner).
"""

import contextlib
import os
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
