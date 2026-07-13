"""Tests for the shared fail-closed auto-merge deferral helper."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from hephaestus.github.auto_merge import defer_auto_merge, defer_auto_merge_batch


def _result(*, state: dict[str, Any] | None = None, returncode: int = 0) -> SimpleNamespace:
    """Build a minimal completed-process substitute for one gh command."""
    return SimpleNamespace(
        returncode=returncode,
        stdout="" if state is None else json.dumps(state),
        stderr="command failed" if returncode else "",
    )


def test_defer_auto_merge_rejects_an_open_state_without_auto_merge_field() -> None:
    """A partial open-state response is never evidence that the PR is unarmed."""
    calls: list[list[str]] = []

    def run(args: list[str]) -> SimpleNamespace:
        calls.append(args)
        return _result(state={"state": "OPEN"})

    assert defer_auto_merge(42, run) is False
    assert calls == [["pr", "view", "42", "--json", "state,autoMergeRequest"]]


def test_defer_auto_merge_rejects_a_disable_command_failure() -> None:
    """A failed disable command cannot be reported as successful containment."""
    responses = iter(
        [
            _result(state={"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            _result(returncode=1),
        ]
    )

    assert defer_auto_merge(42, lambda _args: next(responses)) is False


def test_defer_auto_merge_rejects_a_persisted_auto_merge_arm() -> None:
    """The verification read-back must prove an open PR has no arm."""
    responses = iter(
        [
            _result(state={"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            _result(),
            _result(state={"state": "OPEN", "autoMergeRequest": {"enabledAt": "still"}}),
        ]
    )

    assert defer_auto_merge(42, lambda _args: next(responses)) is False


def test_defer_auto_merge_disables_an_existing_arm_and_verifies_it() -> None:
    """An armed open PR is contained only after an unarmed read-back."""
    calls: list[list[str]] = []
    responses = iter(
        [
            _result(state={"state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}),
            _result(),
            _result(state={"state": "OPEN", "autoMergeRequest": None}),
        ]
    )

    def run(args: list[str]) -> SimpleNamespace:
        calls.append(args)
        return next(responses)

    assert defer_auto_merge(42, run) is True
    assert calls == [
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
        ["pr", "merge", "42", "--disable-auto"],
        ["pr", "view", "42", "--json", "state,autoMergeRequest"],
    ]


def test_defer_auto_merge_batch_attempts_later_prs_after_a_failure() -> None:
    """One unverified sibling must not prevent containment of later PRs."""
    attempted: list[int] = []

    def defer(pr_number: int) -> bool:
        attempted.append(pr_number)
        return pr_number != 41

    assert defer_auto_merge_batch([41, 42], defer) == ["#41"]
    assert attempted == [41, 42]
