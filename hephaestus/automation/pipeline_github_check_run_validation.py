"""Validate GitHub Check Run lifecycle and identity evidence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

_GITHUB_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_CHECK_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_CHECK_CONCLUSIONS = _CHECK_SUCCESS_CONCLUSIONS | frozenset(
    {"action_required", "cancelled", "failure", "stale", "startup_failure", "timed_out"}
)
_CHECK_ACTIVE_STATES = frozenset({"queued", "in_progress", "requested", "waiting", "pending"})
_RequiredCheck = tuple[str, int | None]
_CheckRunCandidate = tuple[datetime, int, dict[str, object]]


@dataclass(frozen=True)
class _ActiveCheckRun:
    """Validated run identity and active lifecycle state."""

    run_id: int
    app_id: int
    status: str


_CheckRunGroups = dict[_RequiredCheck, dict[int, list[_CheckRunCandidate | _ActiveCheckRun]]]


def _valid_app_id(value: object) -> int | None:
    """Return a valid nullable GitHub App ID."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError("GitHub App ID is malformed")


def _check_run_app_id(check_run: dict[str, object]) -> int:
    """Return the positive GitHub App identity for one Check Run."""
    app = check_run.get("app")
    if not isinstance(app, dict):
        raise ValueError("Check Run has no application identity")
    app_id = _valid_app_id(app.get("id"))
    if app_id is None:
        raise ValueError("Check Run has no application identity")
    return app_id


def _check_run_completion_time(value: object) -> tuple[str, datetime] | None:
    """Return one raw completion time and its normalized UTC instant."""
    if not isinstance(value, str) or _GITHUB_TIMESTAMP_RE.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.utcoffset() is None:
            return None
        return value, parsed.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _validated_check_run(
    check_run: dict[str, object], head_sha: str
) -> _ActiveCheckRun | tuple[int, int, str, str, str, datetime] | None:
    """Validate run identity, then lifecycle and applicable terminal fields."""
    try:
        app_id = _check_run_app_id(check_run)
    except ValueError:
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    check_run_id = check_run.get("id")
    if (
        not isinstance(check_run_id, int)
        or isinstance(check_run_id, bool)
        or check_run_id <= 0
        or check_run.get("head_sha") != head_sha
    ):
        logger.warning("Check Run for %s has malformed identity", head_sha)
        return None
    raw_status = check_run.get("status")
    if not isinstance(raw_status, str) or raw_status.lower() not in (
        _CHECK_ACTIVE_STATES | {"completed"}
    ):
        logger.warning("Check Run for %s has malformed lifecycle evidence", head_sha)
        return None
    status = raw_status.lower()
    if status in _CHECK_ACTIVE_STATES:
        return _ActiveCheckRun(check_run_id, app_id, status)
    conclusion = check_run.get("conclusion")
    completion_time = _check_run_completion_time(check_run.get("completed_at"))
    if (
        not isinstance(conclusion, str)
        or conclusion not in _CHECK_CONCLUSIONS
        or completion_time is None
    ):
        logger.warning("Check Run for %s has malformed terminal evidence", head_sha)
        return None
    raw_completed_at, completed_at = completion_time
    return check_run_id, app_id, status, conclusion, raw_completed_at, completed_at


__all__ = [
    "_CHECK_SUCCESS_CONCLUSIONS",
    "_ActiveCheckRun",
    "_CheckRunCandidate",
    "_CheckRunGroups",
    "_RequiredCheck",
    "_check_run_app_id",
    "_validated_check_run",
]
