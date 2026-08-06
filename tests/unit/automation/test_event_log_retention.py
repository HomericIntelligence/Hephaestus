"""Tests for bounded pipeline event-log lifecycle management."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hephaestus.automation import event_log_retention
from hephaestus.automation.event_log_retention import event_log_lifecycle
from hephaestus.utils.file_lock import LockUnavailableError


def _event_log(directory: Path, timestamp: datetime, pid: int) -> Path:
    """Create a named test event log with the supplied UTC timestamp."""
    path = directory / f"pipeline-events-{timestamp.strftime('%Y%m%dT%H%M%SZ')}-{pid}.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_age_retention_uses_strict_cutoff(tmp_path: Path) -> None:
    """Logs exactly at the age boundary remain while older logs expire."""
    now = datetime(2026, 1, 31, 12, tzinfo=UTC)
    current = _event_log(tmp_path, now, 100)
    at_cutoff = _event_log(tmp_path, now - timedelta(days=30), 101)
    expired = _event_log(tmp_path, now - timedelta(days=30, seconds=1), 102)

    with event_log_lifecycle(
        current,
        retention_days=30,
        retention_count=0,
        dry_run=False,
        now=now,
    ):
        pass

    assert at_cutoff.exists()
    assert not expired.exists()


def test_count_retention_removes_oldest_logs_first(tmp_path: Path) -> None:
    """Count retention keeps the newest recognized logs."""
    now = datetime(2026, 1, 4, tzinfo=UTC)
    oldest = _event_log(tmp_path, now - timedelta(days=3), 101)
    second_oldest = _event_log(tmp_path, now - timedelta(days=2), 102)
    newest = _event_log(tmp_path, now - timedelta(days=1), 103)
    current = _event_log(tmp_path, now, 104)

    with event_log_lifecycle(
        current,
        retention_days=0,
        retention_count=2,
        dry_run=False,
        now=now,
    ):
        pass

    assert not oldest.exists()
    assert not second_oldest.exists()
    assert newest.exists()
    assert current.exists()


def test_combined_age_and_count_retention_applies_age_first(tmp_path: Path) -> None:
    """Age expiry happens before count trimming of the remaining set."""
    now = datetime(2026, 1, 10, tzinfo=UTC)
    expired = _event_log(tmp_path, now - timedelta(days=9), 101)
    oldest_retained = _event_log(tmp_path, now - timedelta(days=4), 102)
    newest_retained = _event_log(tmp_path, now - timedelta(days=3), 103)
    current = _event_log(tmp_path, now, 104)

    with event_log_lifecycle(
        current,
        retention_days=5,
        retention_count=2,
        dry_run=False,
        now=now,
    ):
        pass

    assert not expired.exists()
    assert not oldest_retained.exists()
    assert newest_retained.exists()
    assert current.exists()


def test_only_recognized_regular_logs_are_candidates(tmp_path: Path) -> None:
    """Malformed names, directories, and symlinks remain untouched."""
    now = datetime(2026, 1, 31, tzinfo=UTC)
    current = _event_log(tmp_path, now, 100)
    valid_old = _event_log(tmp_path, now - timedelta(days=31), 101)
    malformed_date = tmp_path / "pipeline-events-20260230T000000Z-102.jsonl"
    malformed_date.write_text("{}\n", encoding="utf-8")
    zero_pid = tmp_path / "pipeline-events-20250101T000000Z-0.jsonl"
    zero_pid.write_text("{}\n", encoding="utf-8")
    wrong_suffix = tmp_path / "pipeline-events-20250101T000000Z-103.txt"
    wrong_suffix.write_text("{}\n", encoding="utf-8")
    matching_directory = tmp_path / "pipeline-events-20240101T000000Z-104.jsonl"
    matching_directory.mkdir()
    symlink = tmp_path / "pipeline-events-20240101T000000Z-105.jsonl"
    symlink.symlink_to(wrong_suffix)

    with event_log_lifecycle(
        current,
        retention_days=30,
        retention_count=0,
        dry_run=False,
        now=now,
    ):
        pass

    assert not valid_old.exists()
    assert malformed_date.exists()
    assert zero_pid.exists()
    assert wrong_suffix.exists()
    assert matching_directory.is_dir()
    assert symlink.is_symlink()


def test_active_log_is_not_removed_by_concurrent_cleanup(tmp_path: Path) -> None:
    """A lock held by another lifecycle makes a candidate ineligible."""
    now = datetime(2026, 1, 31, tzinfo=UTC)
    active = _event_log(tmp_path, now - timedelta(days=31), 101)
    current = _event_log(tmp_path, now, 102)

    with event_log_lifecycle(
        active,
        retention_days=0,
        retention_count=0,
        dry_run=False,
        now=now,
    ):
        with event_log_lifecycle(
            current,
            retention_days=30,
            retention_count=0,
            dry_run=False,
            now=now,
        ):
            assert active.exists()

    assert active.exists()


def test_active_logs_can_temporarily_exceed_count_limit(tmp_path: Path) -> None:
    """Concurrent active logs are preserved even when the cap is exceeded."""
    now = datetime(2026, 1, 31, tzinfo=UTC)
    active_one = _event_log(tmp_path, now - timedelta(days=3), 101)
    active_two = _event_log(tmp_path, now - timedelta(days=2), 102)
    current = _event_log(tmp_path, now, 103)

    with event_log_lifecycle(
        active_one,
        retention_days=0,
        retention_count=0,
        dry_run=False,
        now=now,
    ):
        with event_log_lifecycle(
            active_two,
            retention_days=0,
            retention_count=0,
            dry_run=False,
            now=now,
        ):
            with event_log_lifecycle(
                current,
                retention_days=0,
                retention_count=1,
                dry_run=False,
                now=now,
            ):
                pass

    assert active_one.exists()
    assert active_two.exists()
    assert current.exists()


def test_dry_run_previews_removal_without_unlinking(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Dry-run retention logs prospective removals but preserves files."""
    now = datetime(2026, 1, 31, tzinfo=UTC)
    old = _event_log(tmp_path, now - timedelta(days=31), 101)
    current = _event_log(tmp_path, now, 102)

    with caplog.at_level("INFO", logger=event_log_retention.LOG.name):
        with event_log_lifecycle(
            current,
            retention_days=30,
            retention_count=0,
            dry_run=True,
            now=now,
        ):
            pass

    assert old.exists()
    assert any("[dry-run] would remove" in record.message for record in caplog.records)


def test_unlink_failure_is_logged_and_lifecycle_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed unlink does not prevent later candidates from being removed."""
    now = datetime(2026, 1, 31, tzinfo=UTC)
    first = _event_log(tmp_path, now - timedelta(days=32), 101)
    second = _event_log(tmp_path, now - timedelta(days=31), 102)
    current = _event_log(tmp_path, now, 103)
    original_unlink = Path.unlink

    def unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == first:
            raise OSError("simulated unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with caplog.at_level("WARNING", logger=event_log_retention.LOG.name):
        with event_log_lifecycle(
            current,
            retention_days=30,
            retention_count=0,
            dry_run=False,
            now=now,
        ):
            pass

    assert first.exists()
    assert not second.exists()
    assert any("cleanup failed" in record.message for record in caplog.records)


def test_current_lock_failure_is_nonfatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure to acquire the current lock only warns and yields."""

    def unavailable(*args: object, **kwargs: object) -> None:
        raise LockUnavailableError("simulated lock failure")

    monkeypatch.setattr(event_log_retention, "file_lock", unavailable)
    with caplog.at_level("WARNING", logger=event_log_retention.LOG.name):
        with event_log_lifecycle(
            tmp_path / "pipeline-events-20260131T000000Z-101.jsonl",
            retention_days=30,
            retention_count=100,
            dry_run=False,
        ):
            pass

    assert any("cleanup skipped" in record.message for record in caplog.records)


def test_cleanup_lock_failure_is_nonfatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Failure to acquire the directory cleanup lock preserves the files."""
    old = _event_log(tmp_path, datetime(2025, 1, 1, tzinfo=UTC), 101)
    real_file_lock = event_log_retention.file_lock

    @contextmanager
    def fail_cleanup_lock(path: Path, **kwargs: object) -> Iterator[None]:
        if path.name == event_log_retention._RETENTION_LOCK_NAME:
            raise LockUnavailableError("simulated cleanup lock failure")
        with real_file_lock(path, **kwargs):
            yield

    monkeypatch.setattr(event_log_retention, "file_lock", fail_cleanup_lock)
    with caplog.at_level("WARNING", logger=event_log_retention.LOG.name):
        event_log_retention._cleanup_event_logs(
            tmp_path,
            retention_days=30,
            retention_count=0,
            dry_run=False,
            now=datetime(2026, 1, 31, tzinfo=UTC),
        )

    assert old.exists()
    assert any("cleanup skipped" in record.message for record in caplog.records)


def test_zero_limits_disable_cleanup(tmp_path: Path) -> None:
    """Zero disables both retention dimensions without deleting logs."""
    old = _event_log(tmp_path, datetime(2025, 1, 1, tzinfo=UTC), 101)
    current = _event_log(tmp_path, datetime(2026, 1, 31, tzinfo=UTC), 102)

    with event_log_lifecycle(
        current,
        retention_days=0,
        retention_count=0,
        dry_run=False,
        now=datetime(2026, 1, 31, tzinfo=UTC),
    ):
        pass

    assert old.exists()
