"""Tests for :mod:`hephaestus.github.severity_label` (#1210).

These run the SAME parser the auto-label-severity workflow runs, against
sample GitHub-rendered issue bodies, so a body-rendering-format mismatch fails
here instead of silently no-op'ing in CI.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from hephaestus.github import severity_label as sl

# A faithful sample of how GitHub renders an issue-form dropdown answer.
RENDERED_BODY = """\
### Motivation

Something is slow.

### Severity

major

### Parent Epic (optional)

#310
"""


@pytest.mark.parametrize(
    "value,expected",
    [
        ("critical", "critical"),
        ("major", "major"),
        ("minor", "minor"),
        ("nitpick", "nitpick"),
    ],
)
def test_parse_severity_extracts_each_option(value: str, expected: str) -> None:
    """Each rendered severity option is extracted from a faithful body."""
    body = RENDERED_BODY.replace("major", value)
    assert sl.parse_severity(body) == expected


def test_parse_severity_handles_no_response() -> None:
    """The ``_No response_`` placeholder yields ``None`` (safe no-op)."""
    body = RENDERED_BODY.replace("major", "_No response_")
    assert sl.parse_severity(body) is None


def test_parse_severity_blank_lines_between_heading_and_value() -> None:
    """Blank lines between the heading and the value are skipped."""
    body = "### Severity\n\n\nminor\n"
    assert sl.parse_severity(body) == "minor"


def test_parse_severity_missing_section() -> None:
    """A body without a Severity heading yields ``None``."""
    assert sl.parse_severity("### Motivation\n\ntext\n") is None


def test_parse_severity_ignores_non_severity_first_line() -> None:
    """A stray sentence under the heading is not a severity → ``None``."""
    assert sl.parse_severity("### Severity\n\nnot sure really\n") is None


def test_parse_severity_case_insensitive_value() -> None:
    """The selected value is matched case-insensitively and lowercased."""
    assert sl.parse_severity("### Severity\n\nMAJOR\n") == "major"


def test_apply_reconciles_stale_label() -> None:
    """A changed severity deletes the stale label and adds the new one."""
    calls: list[list[str]] = []

    def fake_gh(*args: str) -> str:
        calls.append(list(args))
        if "--jq" in args:
            return "severity:major\nbug\n"
        return ""

    with mock.patch.object(sl, "_gh", side_effect=fake_gh):
        sl.apply_severity_label("o/r", 5, "minor")

    # Stale severity:major deleted; severity:minor added; bug untouched.
    assert any("DELETE" in c and c[-1].endswith("severity:major") for c in calls)
    assert any("labels[]=severity:minor" in c for c in calls)
    assert not any(c[-1].endswith("/labels/bug") for c in calls)


def test_gh_wrapper_delegates_to_gh_call() -> None:
    """The local wrapper routes GitHub CLI calls through the shared adapter."""
    completed = subprocess.CompletedProcess(["gh"], 0, stdout="ok\n", stderr="")
    with mock.patch.object(sl, "gh_call", return_value=completed) as mock_gh_call:
        assert sl._gh("api", "repos/o/r/issues/1/labels") == "ok\n"

    mock_gh_call.assert_called_once_with(
        ["api", "repos/o/r/issues/1/labels"],
        check=True,
    )


def test_apply_idempotent_when_already_correct() -> None:
    """An already-correct label triggers neither a DELETE nor a POST."""

    def fake_gh(*args: str) -> str:
        if "--jq" in args:
            return "severity:minor\nenhancement\n"
        return ""

    with mock.patch.object(sl, "_gh", side_effect=fake_gh) as m:
        sl.apply_severity_label("o/r", 9, "minor")

    assert not any("DELETE" in c.args for c in m.call_args_list)
    assert not any("POST" in c.args for c in m.call_args_list)


def test_apply_no_selection_removes_all_severity() -> None:
    """Clearing the selection removes all ``severity:*`` and adds none."""

    def fake_gh(*args: str) -> str:
        if "--jq" in args:
            return "severity:minor\nenhancement\n"
        return ""

    with mock.patch.object(sl, "_gh", side_effect=fake_gh) as m:
        sl.apply_severity_label("o/r", 7, None)

    deleted = [c for c in m.call_args_list if "DELETE" in c.args]
    assert any(c.args[-1].endswith("severity:minor") for c in deleted)
    assert not any("POST" in c.args for c in m.call_args_list)


def test_main_rejects_non_numeric_issue_number(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric (injection-shaped) issue number exits 1 without API calls."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("ISSUE_NUMBER", "7; rm -rf /")
    monkeypatch.setenv("ISSUE_BODY", "### Severity\n\nmajor\n")
    assert sl.main([]) == 1


def test_main_rejects_missing_github_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing GITHUB_REPOSITORY exits 1 with a descriptive error."""
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    monkeypatch.setenv("ISSUE_BODY", "### Severity\n\nmajor\n")
    assert sl.main([]) == 1


def test_main_rejects_malformed_github_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """A GITHUB_REPOSITORY value without '/' exits 1 with a descriptive error."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "no-slash-here")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    monkeypatch.setenv("ISSUE_BODY", "### Severity\n\nmajor\n")
    assert sl.main([]) == 1


def test_main_reconciles_on_valid_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Valid input parses the severity and reconciles the label."""
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/r")
    monkeypatch.setenv("ISSUE_NUMBER", "42")
    monkeypatch.setenv("ISSUE_BODY", "### Severity\n\nmajor\n")
    with mock.patch.object(sl, "apply_severity_label") as applied:
        assert sl.main([]) == 0
    applied.assert_called_once_with("o/r", 42, "major")


def test_main_help_does_not_touch_env() -> None:
    """``--help`` exits 0 without requiring the workflow env vars (entry-point test)."""
    with pytest.raises(SystemExit) as exc:
        sl.main(["--help"])
    assert exc.value.code == 0
