"""Read the executed-test evidence from a pytest output tail.

The pre-PR test job keeps a bounded tail of each output stream. This module
reads pytest's terminal summary line from that tail, so a passing receipt can
state what ran. It reads text only. It runs no test and it changes no result.

Usage:
    from hephaestus.automation.test_summary import (
        last_pytest_summary_line,
        summary_reports_no_executed_test,
    )

    line = last_pytest_summary_line(job.stdout_tail, job.stderr_tail)
    if line is not None:
        print(line, summary_reports_no_executed_test(line))
"""

from __future__ import annotations

import re

__all__ = ["last_pytest_summary_line", "summary_reports_no_executed_test"]

# pytest prints its terminal summary as a count list, ``in``, and a duration.
# The line can carry ``=`` padding, and a long run appends a clock duration.
_SUMMARY_COUNT = r"\d+ (?:passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
_DURATION = r"\d+(?:\.\d+)?s(?: \(\d+:\d\d:\d\d\))?"
_SUMMARY_LINE = re.compile(
    rf"^[=\s]*(?P<summary>{_SUMMARY_COUNT}(?:,\s*{_SUMMARY_COUNT})* in {_DURATION})[=\s]*$"
)
_NO_TESTS_LINE = re.compile(rf"^[=\s]*(?P<summary>no tests ran in {_DURATION})[=\s]*$")
_COUNT = re.compile(
    r"(?P<count>\d+) "
    r"(?P<kind>passed|failed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
)
# A count token that proves a test body executed. ``skipped``, ``deselected``,
# and ``warning`` are not in this set: they report an outcome with no test run.
_EXECUTED_KINDS = frozenset({"passed", "failed", "error", "xfailed", "xpassed"})


def last_pytest_summary_line(*texts: str | None) -> str | None:
    """Return the last pytest terminal summary line in the supplied texts.

    The scan reads each text from its last line up. The first text that holds a
    summary wins, so a caller can pass stdout before stderr. The return value
    is the summary without the ``=`` padding, for example
    ``"4 passed, 2 warnings in 1.05s"``.

    Args:
        *texts: Output tails to scan, in order of preference. ``None`` and
            empty strings are skipped.

    Returns:
        The summary line, or ``None`` when no text holds one. A truncated tail
        that cut the line off returns ``None``.

    """
    for text in texts:
        if not text:
            continue
        for raw_line in reversed(text.splitlines()):
            stripped = raw_line.strip()
            if not stripped:
                continue
            match = _SUMMARY_LINE.match(stripped) or _NO_TESTS_LINE.match(stripped)
            if match is not None:
                return match.group("summary")
    return None


def summary_reports_no_executed_test(line: str) -> bool:
    """Return whether a summary line proves that no test body executed.

    The check holds only for a line this module recognizes and only when the
    line reports at least one skipped test and no executed outcome. An
    unrecognized line returns ``False``: the caller makes no claim.

    Args:
        line: A summary line, usually the value of
            :func:`last_pytest_summary_line`.

    Returns:
        ``True`` when the line reports skips and no executed test.

    """
    counts = _counts(line)
    if counts is None:
        return False
    if counts.get("skipped", 0) < 1:
        return False
    return not any(counts.get(kind, 0) for kind in _EXECUTED_KINDS)


def _counts(line: str) -> dict[str, int] | None:
    """Return the count per outcome kind, or ``None`` for an unknown line."""
    stripped = line.strip()
    if not (_SUMMARY_LINE.match(stripped) or _NO_TESTS_LINE.match(stripped)):
        return None
    counts: dict[str, int] = {}
    for match in _COUNT.finditer(stripped):
        kind = match.group("kind")
        if kind in {"errors", "warnings"}:
            kind = kind[:-1]
        counts[kind] = counts.get(kind, 0) + int(match.group("count"))
    return counts
