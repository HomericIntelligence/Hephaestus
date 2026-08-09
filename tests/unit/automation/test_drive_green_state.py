"""Behavioral coverage for drive-green durable state transitions."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation.drive_green_state import (
    DriveGreenArmingCoordinator,
    LastCIFixStore,
)


def _coordinator(
    tmp_path: Path,
    *,
    siblings: dict[int, list[int]] | None = None,
    states: dict[int, dict[str, Any] | None] | None = None,
    terminal: str = "PENDING",
) -> tuple[DriveGreenArmingCoordinator, dict[str, list[Any]]]:
    calls: dict[str, list[Any]] = {"learn": [], "compact": [], "mark": [], "status": []}

    def mark(issue: int, record: dict[str, Any], succeeded: bool) -> None:
        calls["mark"].append((issue, succeeded))
        record["learn_status"] = "succeeded" if succeeded else "failed"
        coordinator.store.save(issue, record)

    def learn(issue: int, pr: int) -> bool:
        calls["learn"].append((issue, pr))
        return True

    def compact(issue: int, pr: int) -> bool:
        calls["compact"].append((issue, pr))
        return True

    coordinator = DriveGreenArmingCoordinator(
        state_dir_provider=lambda: tmp_path,
        status_tracker_provider=lambda: SimpleNamespace(
            update_slot=lambda slot, message: calls["status"].append((slot, message))
        ),
        shared_pr_issues_provider=lambda: siblings or {},
        gh_pr_state=lambda pr: (states or {}).get(pr),
        wait_for_pr_terminal=lambda issue, pr: terminal,
        run_drive_green_learnings=learn,
        run_drive_green_compact=compact,
        mark_drive_green_learn_result=mark,
    )
    return coordinator, calls


def _record(*, sha: str = "a" * 40, status: str | None = None) -> dict[str, Any]:
    return {
        "pr_number": 42,
        "pr_head_branch": "feature",
        "head_sha_at_arming": sha,
        "learn_status": status,
        "learn_attempted_at": None,
        "learn_captured_at": None,
        "learn_succeeded_at": None,
    }


def test_last_ci_fix_store_round_trips_only_the_current_head(tmp_path: Path) -> None:
    """A fix marker matches only the exact PR head that was recorded."""
    state = {"headRefOid": "a" * 40}
    store = LastCIFixStore(state_dir_provider=lambda: tmp_path, gh_pr_state=lambda _pr: state)

    assert store.already_pushed_for_current_head(7, 42) is False
    store.record_head(42)
    assert json.loads(store.marker_path(42).read_text()) == {
        "pr_number": 42,
        "head_sha": "a" * 40,
    }
    assert store.already_pushed_for_current_head(7, 42) is True

    state["headRefOid"] = "b" * 40
    assert store.already_pushed_for_current_head(7, 42) is False


@pytest.mark.parametrize("contents", ["not-json", "{}", '{"head_sha": ""}'])
def test_last_ci_fix_store_rejects_invalid_markers(tmp_path: Path, contents: str) -> None:
    """Malformed or incomplete marker contents fail safely as unmatched."""
    store = LastCIFixStore(
        state_dir_provider=lambda: tmp_path,
        gh_pr_state=lambda _pr: {"headRefOid": "a" * 40},
    )
    store.marker_path(42).write_text(contents)

    assert store.already_pushed_for_current_head(7, 42) is False


def test_last_ci_fix_store_ignores_missing_head_and_write_failure(tmp_path: Path) -> None:
    """Unavailable head data and storage failures do not create false markers."""
    store = LastCIFixStore(state_dir_provider=lambda: tmp_path, gh_pr_state=lambda _pr: None)
    store.record_head(42)
    assert not store.marker_path(42).exists()

    store = LastCIFixStore(
        state_dir_provider=lambda: tmp_path,
        gh_pr_state=lambda _pr: {"headRefOid": "a" * 40},
    )
    with patch(
        "hephaestus.automation.drive_green_state.write_secure",
        side_effect=OSError("read only"),
    ):
        store.record_head(42)
    assert not store.marker_path(42).exists()


def test_record_arming_persists_each_sibling_and_preserves_terminal_record(
    tmp_path: Path,
) -> None:
    """Arming persists every sibling without overwriting terminal learning."""
    coordinator, _ = _coordinator(tmp_path, siblings={42: [7, 8]})
    coordinator.store.save(8, _record(status="failed"))

    coordinator.record_arming(42, "feature", "b" * 40)

    first = coordinator.store.load(7)
    second = coordinator.store.load(8)
    assert first is not None and first["head_sha_at_arming"] == "b" * 40
    assert second is not None and second["learn_status"] == "failed"


def test_record_arming_without_linked_issues_writes_nothing(tmp_path: Path) -> None:
    """A PR without linked issues cannot create an ownerless arming record."""
    coordinator, _ = _coordinator(tmp_path)
    coordinator.record_arming(42, "feature", "a" * 40)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("status", ["succeeded", "failed"])
def test_drive_start_stops_for_terminal_learning(tmp_path: Path, status: str) -> None:
    """Completed learning prevents duplicate post-merge processing."""
    coordinator, calls = _coordinator(tmp_path)
    coordinator.store.save(7, _record(status=status))

    result = coordinator.check_on_drive_start(7, 42)

    assert result is not None and result.success
    assert calls["learn"] == []


def test_drive_start_without_record_or_pr_state_has_safe_outcomes(tmp_path: Path) -> None:
    """Missing arming data continues while missing live state stops safely."""
    coordinator, _ = _coordinator(tmp_path)
    assert coordinator.check_on_drive_start(7, 42) is None

    coordinator.store.save(7, _record())
    result = coordinator.check_on_drive_start(7, 42)
    assert result is not None and result.success


def test_drive_start_captures_learning_for_merged_pr(tmp_path: Path) -> None:
    """A merged PR triggers learning, compaction, and durable success."""
    coordinator, calls = _coordinator(tmp_path, states={42: {"state": "MERGED"}})
    coordinator.store.save(7, _record())

    result = coordinator.check_on_drive_start(7, 42)

    assert result is not None and result.success
    assert calls["learn"] == [(7, 42)]
    assert calls["compact"] == [(7, 42)]
    assert calls["mark"] == [(7, True)]
    assert calls["status"] and "capturing post-merge" in calls["status"][0][1]


@pytest.mark.parametrize(
    ("state", "terminal"),
    [("CLOSED", "PENDING"), ("OPEN", "CLOSED"), ("OPEN", "FAILING"), ("OPEN", "DIRTY")],
)
def test_drive_start_clears_records_that_must_reenter_normal_work(
    tmp_path: Path, state: str, terminal: str
) -> None:
    """Terminal or actionable PR states clear obsolete arming records."""
    coordinator, _ = _coordinator(tmp_path, states={42: {"state": state}}, terminal=terminal)
    coordinator.store.save(7, _record())

    assert coordinator.check_on_drive_start(7, 42) is None
    assert coordinator.store.load(7) is None


def test_drive_start_clears_arming_when_head_advanced(tmp_path: Path) -> None:
    """A changed PR head invalidates the earlier arming proof."""
    coordinator, _ = _coordinator(
        tmp_path,
        states={42: {"state": "OPEN", "headRefOid": "b" * 40}},
    )
    coordinator.store.save(7, _record(sha="a" * 40))

    assert coordinator.check_on_drive_start(7, 42) is None
    assert coordinator.store.load(7) is None


def test_drive_start_wait_merge_captures_and_pending_remains_armed(tmp_path: Path) -> None:
    """Merge completion learns while pending merge retains its arming record."""
    merged, merged_calls = _coordinator(
        tmp_path,
        states={42: {"state": "OPEN", "headRefOid": "a" * 40}},
        terminal="MERGED",
    )
    merged.store.save(7, _record())
    result = merged.check_on_drive_start(7, 42)
    assert result is not None and result.success
    assert merged_calls["learn"] == [(7, 42)]

    pending, _ = _coordinator(
        tmp_path,
        states={42: {"state": "OPEN", "headRefOid": "a" * 40}},
    )
    pending.store.save(8, _record())
    result = pending.check_on_drive_start(8, 42)
    assert result is not None and result.success
    assert pending.store.load(8) is not None


def test_orphan_sweep_captures_merged_and_clears_closed_records(tmp_path: Path) -> None:
    """The orphan sweep learns merged records and removes closed ones."""
    coordinator, calls = _coordinator(
        tmp_path,
        states={42: {"state": "MERGED"}, 43: {"state": "CLOSED"}, 44: None},
    )
    coordinator.store.save(7, _record())
    closed = _record()
    closed["pr_number"] = 43
    coordinator.store.save(8, closed)
    unknown = _record()
    unknown["pr_number"] = 44
    coordinator.store.save(9, unknown)

    coordinator.sweep_orphaned_records()

    assert calls["learn"] == [(7, 42)]
    assert coordinator.store.load(8) is None
    assert coordinator.store.load(9) is not None


def test_orphan_sweep_ignores_malformed_and_drops_invalid_pr_number(tmp_path: Path) -> None:
    """Malformed files are isolated and invalid persisted PR numbers removed."""
    coordinator, _ = _coordinator(tmp_path)
    (tmp_path / "drive-green-armed-bad.json").write_text("{}")
    coordinator.store.save(7, {"pr_number": "42"})

    coordinator.sweep_orphaned_records()

    assert (tmp_path / "drive-green-armed-bad.json").exists()
    assert coordinator.store.load(7) is None


def test_terminal_record_recognizes_legacy_timestamps() -> None:
    """Legacy success timestamps remain terminal for compatibility."""
    assert DriveGreenArmingCoordinator._learn_record_terminal({"learn_captured_at": "now"})
    assert DriveGreenArmingCoordinator._learn_record_terminal({"learn_succeeded_at": "now"})
    assert not DriveGreenArmingCoordinator._learn_record_terminal({})
