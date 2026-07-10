"""Single source of truth for the required-checks-gate fan-in invariant.

The ``required-checks-gate`` job in ``.github/workflows/_required.yml`` is an
aggregate workflow signal and a classic branch-protection required context. It
must fan in every gating job via its ``needs:`` list so aggregate coverage
remains complete alongside the direct GitHub ruleset contexts.
``_unwired_jobs`` computes which jobs are *not* wired into the gate, and is
shared by the structural guard tests so the guard and its negative-path test
exercise one code path and cannot diverge (issue #1338).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

GATE_JOB = "required-checks-gate"


def _unwired_jobs(
    wf: dict[str, Any],
    excluded: Iterable[str],
    *,
    gate_job: str = GATE_JOB,
) -> set[str]:
    """Return jobs defined in ``wf`` but absent from the gate's ``needs:`` list.

    Args:
        wf: A parsed GitHub Actions workflow document (the mapping produced by
            ``yaml.safe_load``). Must contain a ``jobs`` mapping that includes
            ``gate_job`` with a ``needs`` list.
        excluded: Job names intentionally not gated (e.g. advisory jobs and the
            gate itself); these are removed from the result.
        gate_job: Name of the aggregating gate job. Defaults to
            ``required-checks-gate``.

    Returns:
        The set of job names that are defined in ``wf['jobs']`` but neither
        listed in ``wf['jobs'][gate_job]['needs']`` nor in ``excluded``. An
        empty set means every gating job is wired into the gate.

    """
    jobs = wf["jobs"]
    gate_needs = set(jobs[gate_job]["needs"])
    return (set(jobs) - set(excluded)) - gate_needs
