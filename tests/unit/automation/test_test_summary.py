"""Tests for the pytest terminal-summary reader (issue #3451).

The reader supplies the evidence a passing test receipt quotes. These tests use
real pytest terminal output lines, including the padded form, the long-duration
form, and a tail that a truncation cut off.
"""

from __future__ import annotations

import pytest

from hephaestus.automation.test_summary import (
    last_pytest_summary_line,
    summary_reports_no_executed_test,
)


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        ("tests/unit/test_a.py .\n\n1 passed in 0.12s\n", "1 passed in 0.12s"),
        ("4 passed, 2 warnings in 1.05s\n", "4 passed, 2 warnings in 1.05s"),
        ("===== 1 passed in 0.12s =====\n", "1 passed in 0.12s"),
        ("2 failed, 3 passed in 61.23s (0:01:01)\n", "2 failed, 3 passed in 61.23s (0:01:01)"),
        ("..\n1 skipped in 0.02s\n", "1 skipped in 0.02s"),
        ("no tests ran in 0.01s\n", "no tests ran in 0.01s"),
    ],
)
def test_summary_line_is_read_from_the_tail(tail: str, expected: str) -> None:
    """Each terminal summary form is recognized and quoted without padding."""
    assert last_pytest_summary_line(tail) == expected


def test_reader_returns_the_last_summary_not_an_earlier_one() -> None:
    """A container command can run pytest more than once; the last line wins."""
    tail = "1 passed in 0.10s\nrun two\n5 passed in 0.90s\n"
    assert last_pytest_summary_line(tail) == "5 passed in 0.90s"


def test_reader_skips_trailing_lines_after_the_summary() -> None:
    """A coverage note after the summary does not hide it."""
    tail = "3 passed in 0.40s\nRequired test coverage of 85% reached.\n"
    assert last_pytest_summary_line(tail) == "3 passed in 0.40s"


def test_reader_uses_the_first_text_that_holds_a_summary() -> None:
    """The first text wins, and a summary-free first text falls through."""
    assert last_pytest_summary_line("1 passed in 0.10s\n", "2 passed in 0.20s\n") == (
        "1 passed in 0.10s"
    )
    assert last_pytest_summary_line("collected 4 items\n", "1 passed in 0.10s\n") == (
        "1 passed in 0.10s"
    )


@pytest.mark.parametrize("texts", [(), ("", None), ("tests/a.py ..\n", None)])
def test_reader_makes_no_claim_when_no_summary_exists(texts: tuple[str | None, ...]) -> None:
    """An empty or unreadable tail returns no summary."""
    assert last_pytest_summary_line(*texts) is None


def test_reader_makes_no_claim_for_a_truncated_summary_line() -> None:
    """A tail that cut the line off holds no complete summary."""
    tail = "tests/a.py::test_one PASSED\ntests/a.py::test_two\n4 passed in"
    assert last_pytest_summary_line(tail) is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("1 skipped in 0.02s", True),
        ("4 skipped, 1 warning in 0.03s", True),
        ("1 passed in 0.12s", False),
        ("3 skipped, 1 passed in 0.30s", False),
        ("2 skipped, 1 failed in 0.30s", False),
        ("1 skipped, 1 xfailed in 0.30s", False),
        ("1 error in 0.30s", False),
        ("1 deselected in 0.01s", False),
        ("no tests ran in 0.01s", False),
        ("1 passed in", False),
        ("not a summary line", False),
    ],
)
def test_no_execution_rule_needs_skips_and_no_executed_outcome(line: str, expected: bool) -> None:
    """Only a recognized all-skip summary proves that no test body executed."""
    assert summary_reports_no_executed_test(line) is expected
