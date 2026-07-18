"""Shared fail-closed auto-merge containment helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

_PR_STATE_FIELDS = "state,autoMergeRequest"
_TERMINAL_STATES = frozenset({"MERGED", "CLOSED"})


def _read_auto_merge_state(pr_number: int, run: Callable[[list[str]], Any]) -> tuple[str, bool]:
    """Read a complete PR state response or raise without inferring safety."""
    result = run(["pr", "view", str(pr_number), "--json", _PR_STATE_FIELDS])
    if getattr(result, "returncode", 0) != 0:
        raise RuntimeError(getattr(result, "stderr", "") or "gh pr view failed")
    try:
        data = json.loads(getattr(result, "stdout", "") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("invalid PR state response") from exc
    if not isinstance(data, dict) or "state" not in data or "autoMergeRequest" not in data:
        raise RuntimeError("incomplete PR state response")
    state = str(data["state"] or "").upper()
    if state not in _TERMINAL_STATES | {"OPEN"}:
        raise RuntimeError(f"unexpected PR state {state!r}")
    return state, data["autoMergeRequest"] is not None


def defer_auto_merge(pr_number: int, run: Callable[[list[str]], Any]) -> bool:
    """Disable a PR auto-merge request and verify containment by read-back.

    ``run`` executes a raw ``gh`` argument vector for the caller's repository
    context. Any malformed state, command failure, or failed read-back returns
    ``False`` so every caller can stop at its own fail-closed boundary.
    """
    try:
        state, armed = _read_auto_merge_state(pr_number, run)
        if state in _TERMINAL_STATES:
            return True
        if not armed:
            return True
        disabled = run(["pr", "merge", str(pr_number), "--disable-auto"])
        if getattr(disabled, "returncode", 0) != 0:
            raise RuntimeError(
                getattr(disabled, "stderr", "") or "gh pr merge --disable-auto failed"
            )
        verified_state, verified_armed = _read_auto_merge_state(pr_number, run)
        if verified_state in _TERMINAL_STATES:
            return True
        if verified_state == "OPEN" and not verified_armed:
            logger.warning("Disabled auto-merge for PR #%s pending the PR-review gate", pr_number)
            return True
        raise RuntimeError("auto-merge remains enabled")
    except Exception as exc:
        logger.error("Could not verify auto-merge disabled for PR #%s: %s", pr_number, exc)
        return False


def defer_auto_merge_batch(
    pr_numbers: Iterable[int], defer: Callable[[int], bool | None]
) -> list[str]:
    """Attempt containment for every known PR and return failed numbers.

    A failure for one same-head PR must not leave later PRs unexamined: they
    may have independent auto-merge requests. Callers raise only after this
    function has attempted every supplied number.
    """
    failures: list[str] = []
    for pr_number in pr_numbers:
        try:
            if defer(pr_number) is False:
                failures.append(f"#{pr_number}")
        except Exception as exc:
            failures.append(f"#{pr_number}: {exc}")
    return failures
