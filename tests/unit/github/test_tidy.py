"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches."""

from __future__ import annotations

import argparse

import pytest

from hephaestus.github.tidy import _build_arg_parser, _print_summary, parse_problem_branches

# Fixture: clean gh-tidy run (no problem branches)
CLEAN_OUTPUT = """\
Checking out main and pulling the latest from remote origin...
Finished tidying!
"""

# Fixture: one problem branch
ONE_PROBLEM = """\
Rebasing ALL local branches on to latest master...
Rebasing feature/my-branch...
WARNING: Problem rebasing feature/my-branch
Finished rebasing!

Cleaning unnecessary files & optimizing your local repo...
WARNING: Unable to auto-rebase the following branches:
    * feature/my-branch

Finished tidying!
"""

# Fixture: multiple problem branches
MULTI_PROBLEM = """\
WARNING: Unable to auto-rebase the following branches:
    * feature/alpha
    * fix/beta-crash
    * chore/deps-update

Finished tidying!
"""

# Fixture: ANSI-coloured output (gh-tidy emits \e[93m yellow for warnings)
ANSI_PROBLEM = (
    "\x1b[93mWARNING: Unable to auto-rebase the following branches:\x1b[0m\n"
    "\x1b[93m    * feature/with-ansi\x1b[0m\n"
    "\x1b[92mFinished tidying!\x1b[0m\n"
)

# Fixture: problem header with no bullets (edge case — header present, no branch listed)
EMPTY_PROBLEM_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:

Finished tidying!
"""

# Fixture: problem header where a non-bullet line immediately follows
TRAILING_TEXT_AFTER_BLOCK = """\
WARNING: Unable to auto-rebase the following branches:
    * chore/broken
Please fix manually.
Finished tidying!
"""


def test_clean_output_returns_empty():
    """Clean gh-tidy output with no problem branches returns empty list."""
    assert parse_problem_branches(CLEAN_OUTPUT) == []


def test_single_problem_branch():
    """Single problem branch is extracted correctly."""
    result = parse_problem_branches(ONE_PROBLEM)
    assert result == ["feature/my-branch"]


def test_multiple_problem_branches():
    """Multiple problem branches are all extracted."""
    result = parse_problem_branches(MULTI_PROBLEM)
    assert result == ["feature/alpha", "fix/beta-crash", "chore/deps-update"]


def test_ansi_codes_stripped():
    """ANSI colour codes are stripped before parsing branch names."""
    result = parse_problem_branches(ANSI_PROBLEM)
    assert result == ["feature/with-ansi"]


def test_empty_problem_block():
    """Problem header with no bullets returns empty list."""
    result = parse_problem_branches(EMPTY_PROBLEM_BLOCK)
    assert result == []


def test_trailing_text_terminates_block():
    """Non-bullet line after the branch list terminates parsing."""
    result = parse_problem_branches(TRAILING_TEXT_AFTER_BLOCK)
    assert result == ["chore/broken"]


def test_no_problem_header_at_all():
    """Output without the problem header returns empty list."""
    result = parse_problem_branches("Finished tidying!\n")
    assert result == []


@pytest.mark.parametrize(
    "branch",
    [
        "main",
        "feature/foo-bar",
        "fix/issue-123",
        "chore/bump-deps",
        "release/v2.0.0",
    ],
)
def test_various_branch_name_formats(branch: str):
    """Various branch name formats are all parsed correctly."""
    output = (
        "WARNING: Unable to auto-rebase the following branches:\n"
        f"    * {branch}\n"
        "Finished tidying!\n"
    )
    assert parse_problem_branches(output) == [branch]


# ---------------------------------------------------------------------------
# _print_summary tests
# ---------------------------------------------------------------------------


def test_print_summary_all_rebased_returns_zero():
    """All branches rebased → exit code 0."""
    results = {"feat/a": "rebased", "fix/b": "rebased"}
    assert _print_summary(results) == 0


def test_print_summary_all_subsumed_returns_zero():
    """All branches subsumed → exit code 0."""
    results = {"feat/a": "subsumed"}
    assert _print_summary(results) == 0


def test_print_summary_with_failures_returns_one():
    """Any branch that is not rebased/subsumed/dry-run → exit code 1."""
    results = {"feat/a": "rebased", "fix/b": "failed"}
    assert _print_summary(results) == 1


def test_print_summary_dry_run_counts_as_no_failure():
    """dry-run status is not counted as a failure."""
    results = {"feat/a": "dry-run"}
    assert _print_summary(results) == 0


def test_print_summary_empty_returns_zero():
    """Empty results → exit code 0."""
    assert _print_summary({}) == 0


# ---------------------------------------------------------------------------
# _build_arg_parser tests
# ---------------------------------------------------------------------------


def test_build_arg_parser_returns_parser():
    """_build_arg_parser returns an ArgumentParser."""
    parser = _build_arg_parser()
    assert isinstance(parser, argparse.ArgumentParser)


def test_arg_parser_defaults():
    """Default parsed args have expected values."""
    parser = _build_arg_parser()
    args = parser.parse_args([])
    assert args.dry_run is False
    assert args.trunk is None
    assert args.no_swarm is False
    assert args.max_concurrent == 5
    assert args.verbose is False


def test_arg_parser_accepts_dry_run():
    """--dry-run flag is accepted and sets dry_run to True."""
    parser = _build_arg_parser()
    args = parser.parse_args(["--dry-run"])
    assert args.dry_run is True


def test_arg_parser_accepts_trunk():
    """--trunk BRANCH sets the trunk attribute."""
    parser = _build_arg_parser()
    args = parser.parse_args(["--trunk", "develop"])
    assert args.trunk == "develop"


def test_arg_parser_accepts_no_swarm():
    """--no-swarm flag is accepted."""
    parser = _build_arg_parser()
    args = parser.parse_args(["--no-swarm"])
    assert args.no_swarm is True


def test_arg_parser_accepts_max_concurrent():
    """--max-concurrent N sets max_concurrent to N."""
    parser = _build_arg_parser()
    args = parser.parse_args(["--max-concurrent", "3"])
    assert args.max_concurrent == 3
