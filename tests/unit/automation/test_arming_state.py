"""Unit tests for :class:`hephaestus.automation.arming_state.ArmingStateStore`.

Characterizes the on-disk drive-green arming-record contract extracted from
``CIDriver`` as part of the #1178 decomposition. Mirrors the behavior the
inline ``_load/_save/_clear_arming_state`` methods provided.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.arming_state import ArmingStateStore


def _store(state_dir: Path) -> ArmingStateStore:
    """Build a store whose provider returns ``state_dir`` (mirrors the driver)."""
    return ArmingStateStore(lambda: state_dir)


def test_path_filename_format(tmp_path: Path) -> None:
    """path() yields the ``drive-green-armed-<n>.json`` filename."""
    store = _store(tmp_path)
    assert store.path(123) == tmp_path / "drive-green-armed-123.json"


def test_learn_claim_lock_uses_a_stable_sibling_path(tmp_path: Path) -> None:
    """The claim lock is not the JSON inode that atomic writes replace."""
    store = _store(tmp_path)
    assert store.learn_claim_lock_path(123) == tmp_path / "drive-green-armed-123.learn.lock"


def test_provider_resolved_live_not_at_construction(tmp_path: Path) -> None:
    """The store reads state_dir on each call, following reassignment."""
    # The driver reassigns ``state_dir`` after __init__; the store must follow.
    current = {"dir": tmp_path / "first"}
    current["dir"].mkdir()
    store = ArmingStateStore(lambda: current["dir"])
    moved = tmp_path / "second"
    moved.mkdir()
    current["dir"] = moved
    store.save(7, {"pr": 1})
    assert (moved / "drive-green-armed-7.json").exists()
    assert store.load(7) == {"pr": 1}


def test_load_missing_returns_none(tmp_path: Path) -> None:
    """load() returns None when no record exists for the issue."""
    store = _store(tmp_path)
    assert store.load(123) is None


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    """A saved record reads back identically."""
    store = _store(tmp_path)
    store.save(42, {"pr": 7, "sha": "abc"})
    assert store.load(42) == {"pr": 7, "sha": "abc"}


def test_load_corrupt_json_returns_none(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Corrupt JSON yields None and a WARNING, never an exception."""
    store = _store(tmp_path)
    store.path(5).write_text("{not json")
    with caplog.at_level("WARNING"):
        assert store.load(5) is None
    # load() now routes through the canonical load_state_file helper (#1432),
    # which emits the unified "Malformed <prefix> state" warning.
    assert "Malformed drive-green-armed state" in caplog.text


def test_save_unwritable_dir_swallows_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """save() swallows OSError and logs a WARNING when the dir is unwritable."""
    # state_dir points at a path whose parent is a file, so write_text raises OSError.
    not_a_dir = tmp_path / "afile"
    not_a_dir.write_text("x")
    store = _store(not_a_dir)
    with caplog.at_level("WARNING"):
        store.save(9, {"pr": 1})  # must not raise
    assert "Could not write arming record" in caplog.text


def test_clear_missing_does_not_raise(tmp_path: Path) -> None:
    """clear() on a missing record is a no-op (missing_ok semantics)."""
    store = _store(tmp_path)
    store.clear(404)  # missing_ok semantics — no raise


def test_clear_existing_removes_file(tmp_path: Path) -> None:
    """clear() deletes an existing record file."""
    store = _store(tmp_path)
    store.save(11, {"pr": 3})
    assert store.path(11).exists()
    store.clear(11)
    assert not store.path(11).exists()
