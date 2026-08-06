"""Bounded lifecycle management for pipeline JSONL event logs.

Event logs are diagnostic artifacts, not pipeline state.  This module protects
the log for the current run and performs best-effort cleanup of recognized,
inactive sibling logs without allowing cleanup failures to affect routing.
"""

from __future__ import annotations

import logging
import re
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hephaestus.utils.file_lock import LockUnavailableError, file_lock

LOG = logging.getLogger(__name__)

DEFAULT_EVENT_LOG_RETENTION_DAYS = 30
DEFAULT_EVENT_LOG_RETENTION_COUNT = 100

_EVENT_LOG_NAME = re.compile(
    r"\Apipeline-events-(?P<timestamp>\d{8}T\d{6}Z)-(?P<pid>[1-9]\d*)\.jsonl\Z"
)
_RETENTION_LOCK_NAME = ".pipeline-events-retention.lock"


@dataclass(frozen=True)
class _EventLog:
    """A recognized regular event-log file and its name-derived timestamp."""

    path: Path
    timestamp: datetime


def _parse_event_log(path: Path) -> _EventLog | None:
    """Parse a recognized event-log name, returning ``None`` otherwise."""
    match = _EVENT_LOG_NAME.fullmatch(path.name)
    if match is None:
        return None
    try:
        timestamp = datetime.strptime(match.group("timestamp"), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return _EventLog(path=path, timestamp=timestamp.replace(tzinfo=UTC))


def _scan_event_logs(directory: Path) -> list[_EventLog]:
    """Return recognized regular, non-symlink logs in ``directory``."""
    try:
        entries = directory.iterdir()
        logs: list[_EventLog] = []
        for entry in entries:
            parsed = _parse_event_log(entry)
            if parsed is None:
                continue
            try:
                mode = entry.lstat().st_mode
            except OSError as exc:
                LOG.warning("pipeline event-log stat failed for %s: %s", entry, exc)
                continue
            if stat.S_ISREG(mode):
                logs.append(parsed)
    except OSError as exc:
        LOG.warning("pipeline event-log cleanup scan failed for %s: %s", directory, exc)
        return []
    return logs


def _remove_event_log(log: _EventLog, *, dry_run: bool) -> bool:
    """Remove one inactive log while holding its non-blocking lock."""
    try:
        mode = log.path.lstat().st_mode
    except OSError as exc:
        LOG.warning("pipeline event-log stat failed for %s: %s", log.path, exc)
        return False
    if not stat.S_ISREG(mode):
        return False

    try:
        with file_lock(log.path, blocking=False, require_exclusive=True):
            try:
                mode = log.path.lstat().st_mode
            except OSError as exc:
                LOG.warning("pipeline event-log stat failed for %s: %s", log.path, exc)
                return False
            if not stat.S_ISREG(mode):
                return False
            if dry_run:
                LOG.info("[dry-run] would remove pipeline event log %s", log.path)
                return True
            log.path.unlink()
            LOG.info("removed pipeline event log %s", log.path)
            return True
    except LockUnavailableError:
        LOG.info("skipping active pipeline event log %s", log.path)
        return False
    except (OSError, RuntimeError) as exc:
        LOG.warning("pipeline event-log cleanup failed for %s: %s", log.path, exc)
        return False


def _age_candidates(
    logs: list[_EventLog], cutoff: datetime | None, current_name: str | None
) -> list[_EventLog]:
    """Return inactive logs older than ``cutoff`` in oldest-first order."""
    if cutoff is None:
        return []
    return [
        log
        for log in logs
        if log.timestamp < cutoff and log.path.name != current_name
    ]


def _count_candidates(
    logs: list[_EventLog],
    *,
    current_name: str | None,
    removed: set[Path],
    attempted: set[Path],
) -> list[_EventLog]:
    """Return all oldest-first logs still eligible for count retention."""
    return [
        log
        for log in logs
        if log.path not in removed
        and log.path not in attempted
        and log.path.name != current_name
    ]


def _cleanup_event_logs(
    directory: Path,
    *,
    retention_days: int,
    retention_count: int,
    dry_run: bool,
    now: datetime,
    current_path: Path | None = None,
) -> None:
    """Apply age and count retention under a directory-wide cleanup lock."""
    if retention_days == 0 and retention_count == 0:
        return

    try:
        with file_lock(
            directory / _RETENTION_LOCK_NAME,
            require_exclusive=True,
        ):
            logs = sorted(_scan_event_logs(directory), key=lambda log: log.timestamp)
            current_name = current_path.name if current_path is not None else None
            removed: set[Path] = set()
            attempted: set[Path] = set()

            cutoff = (
                now - timedelta(days=retention_days) if retention_days > 0 else None
            )
            for log in _age_candidates(logs, cutoff, current_name):
                attempted.add(log.path)
                if _remove_event_log(log, dry_run=dry_run):
                    removed.add(log.path)

            if retention_count > 0:
                for log in _count_candidates(
                    logs,
                    current_name=current_name,
                    removed=removed,
                    attempted=attempted,
                ):
                    needed = max(0, len(logs) - len(removed) - retention_count)
                    if needed == 0:
                        break
                    attempted.add(log.path)
                    if _remove_event_log(log, dry_run=dry_run):
                        removed.add(log.path)
    except (LockUnavailableError, OSError, RuntimeError) as exc:
        LOG.warning("pipeline event-log cleanup skipped: %s", exc)


@contextmanager
def event_log_lifecycle(
    path: Path | None,
    *,
    retention_days: int,
    retention_count: int,
    dry_run: bool,
    now: datetime | None = None,
) -> Iterator[None]:
    """Protect the active log and prune only locked, recognized inactive logs.

    Cleanup is deliberately best effort.  Lock, scan, stat, and unlink
    failures are logged and leave the pipeline's execution and exit status
    unchanged.
    """
    if path is None:
        yield
        return

    active_lock = ExitStack()
    try:
        active_lock.enter_context(
            file_lock(path, blocking=False, require_exclusive=True)
        )
    except (LockUnavailableError, OSError, RuntimeError) as exc:
        LOG.warning("pipeline event-log cleanup skipped: %s", exc)
        yield
        return

    try:
        try:
            cleanup_now = now if now is not None else datetime.now(UTC)
            if cleanup_now.tzinfo is None:
                cleanup_now = cleanup_now.replace(tzinfo=UTC)
            else:
                cleanup_now = cleanup_now.astimezone(UTC)
            _cleanup_event_logs(
                path.parent,
                retention_days=retention_days,
                retention_count=retention_count,
                dry_run=dry_run,
                now=cleanup_now,
                current_path=path,
            )
        except Exception as exc:  # pragma: no cover - defensive final boundary
            LOG.warning("pipeline event-log cleanup failed: %s", exc)
        yield
    finally:
        try:
            active_lock.close()
        except Exception as exc:  # pragma: no cover - defensive final boundary
            LOG.warning("pipeline event-log lock release failed: %s", exc)
