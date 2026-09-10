"""Convert completed worker futures to bounded completion records."""

from collections.abc import Callable
from concurrent.futures import Future

from .job_results import JobResult


def resolve_worker_future(
    future: Future[JobResult],
    *,
    crash_result: Callable[[BaseException], JobResult],
    cancelled_result: JobResult,
) -> JobResult:
    """Return the result, or apply the pool's cancellation and crash policies."""
    if future.cancelled():
        return cancelled_result
    try:
        return future.result()
    except BaseException as error:
        return crash_result(error)
