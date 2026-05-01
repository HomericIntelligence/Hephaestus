"""Unit tests for hephaestus.github.tidy — focusing on parse_problem_branches."""

import pytest

from hephaestus.github.tidy import parse_problem_branches


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


def test_clean_output_returns_empty() -> None:
    """No problem branches when output is a clean run."""
    assert parse_problem_branches(CLEAN_OUTPUT) == []


def test_single_problem_branch() -> None:
    """Single problem branch is extracted correctly."""
    result = parse_problem_branches(ONE_PROBLEM)
    assert result == ["feature/my-branch"]


def test_multiple_problem_branches() -> None:
    """All branches listed under the warning header are returned."""
    result = parse_problem_branches(MULTI_PROBLEM)
    assert result == ["feature/alpha", "fix/beta-crash", "chore/deps-update"]


def test_ansi_codes_stripped() -> None:
    """ANSI escape sequences are stripped before parsing."""
    result = parse_problem_branches(ANSI_PROBLEM)
    assert result == ["feature/with-ansi"]


def test_empty_problem_block() -> None:
    """Warning header with no bullet lines returns empty list."""
    result = parse_problem_branches(EMPTY_PROBLEM_BLOCK)
    assert result == []


def test_trailing_text_terminates_block() -> None:
    """Non-bullet line after the branch list terminates parsing."""
    result = parse_problem_branches(TRAILING_TEXT_AFTER_BLOCK)
    assert result == ["chore/broken"]


def test_no_problem_header_at_all() -> None:
    """Output with no warning header returns empty list."""
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
def test_various_branch_name_formats(branch: str) -> None:
    """Branch names with slashes, numbers, and hyphens are all parsed correctly."""
    output = (
        "WARNING: Unable to auto-rebase the following branches:\n"
        f"    * {branch}\n"
        "Finished tidying!\n"
    )
    assert parse_problem_branches(output) == [branch]
