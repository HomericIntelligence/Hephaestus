"""Stable exact-head commit-status reads for the merge gate."""

from __future__ import annotations

import json
import logging
import time
from threading import Event

import hephaestus.automation.github_api as github_api

from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_STATUS_PAGE_SIZE = 100
_STATUS_MAX_TOTAL_COUNT = 2_000
_STATUS_STATES = frozenset({"error", "failure", "pending", "success"})
_RequiredCheck = tuple[str, int | None]
_StatusSnapshot = tuple[tuple[int, str, str], ...]


def _status_page(payload: object, head_sha: str) -> tuple[int, list[object]] | None:
    """Validate one combined-status response page."""
    if not isinstance(payload, dict) or payload.get("sha") != head_sha:
        logger.warning("Commit-status response does not match reviewed head %s", head_sha)
        return None
    total_count = payload.get("total_count")
    statuses = payload.get("statuses")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
        or not isinstance(statuses, list)
        or len(statuses) > _STATUS_PAGE_SIZE
    ):
        logger.warning("Commit-status response for %s is malformed", head_sha)
        return None
    return total_count, statuses


def _statuses_for_head(
    host: _PipelineGitHubHost,
    head_sha: str,
    *,
    deadline_s: float,
    cancellation: Event,
) -> list[object] | None:
    """Read every combined-status page within one aggregate budget."""
    owner, name = host._owner_name()
    endpoint = f"/repos/{owner}/{name}/commits/{head_sha}/status?per_page={_STATUS_PAGE_SIZE}"
    statuses: list[object] = []
    expected_count: int | None = None
    status_ids: set[int] = set()
    page = 1
    while expected_count is None or len(statuses) < expected_count:
        if cancellation.is_set():
            return None
        remaining = deadline_s - time.monotonic()
        if remaining <= 0:
            return None
        page_endpoint = endpoint if page == 1 else f"{endpoint}&page={page}"
        result = github_api.gh_call(
            ["api", page_endpoint],
            check=False,
            timeout=min(float(host._gh_timeout), remaining),
        )
        if result.returncode != 0:
            raise RuntimeError("GitHub returned an error for commit statuses")
        parsed = _status_page(json.loads(result.stdout or "null"), head_sha)
        if parsed is None:
            return None
        total_count, page_statuses = parsed
        if expected_count is None:
            expected_count = total_count
            if expected_count > _STATUS_MAX_TOTAL_COUNT:
                logger.warning(
                    "Commit-status response exceeds the %d-status safety ceiling for %s",
                    _STATUS_MAX_TOTAL_COUNT,
                    head_sha,
                )
                return None
        elif total_count != expected_count:
            logger.warning("Commit-status count changed for %s", head_sha)
            return None
        for status in page_statuses:
            status_id = status.get("id") if isinstance(status, dict) else None
            if (
                not isinstance(status_id, int)
                or isinstance(status_id, bool)
                or status_id <= 0
                or status_id in status_ids
            ):
                logger.warning("Commit-status page has invalid identity for %s", head_sha)
                return None
            status_ids.add(status_id)
        statuses.extend(page_statuses)
        if len(statuses) > expected_count or (not page_statuses and len(statuses) < expected_count):
            logger.warning("Commit-status pages are incomplete for %s", head_sha)
            return None
        page += 1
    return statuses


def _status_snapshot(
    statuses: list[object],
    required_checks: frozenset[_RequiredCheck],
    head_sha: str,
) -> tuple[_StatusSnapshot, frozenset[_RequiredCheck]] | None:
    """Return required commit-status evidence and its stable identity."""
    required_names = {context for context, _app_id in required_checks}
    seen_contexts: set[str] = set()
    snapshot: list[tuple[int, str, str]] = []
    matched: set[_RequiredCheck] = set()
    for status in statuses:
        if not isinstance(status, dict):
            return None
        status_id = status.get("id")
        context = status.get("context")
        state = status.get("state")
        if (
            not isinstance(status_id, int)
            or isinstance(status_id, bool)
            or status_id <= 0
            or not isinstance(context, str)
            or not context
            or state not in _STATUS_STATES
        ):
            logger.warning("Commit status for %s is malformed", head_sha)
            return None
        if context not in required_names:
            continue
        if context in seen_contexts:
            logger.warning("Commit statuses contain duplicate context %s", context)
            return None
        seen_contexts.add(context)
        normalized_state = str(state)
        snapshot.append((status_id, context, normalized_state))
        if normalized_state != "success":
            return None
        matched.update(
            requirement for requirement in required_checks if requirement == (context, None)
        )
    return tuple(sorted(snapshot)), frozenset(matched)


def stable_passing_commit_status_requirements(
    host: _PipelineGitHubHost,
    head_sha: str,
    required_checks: frozenset[_RequiredCheck],
    *,
    deadline_s: float,
    cancellation: Event,
) -> frozenset[_RequiredCheck] | None:
    """Return requirements proved by two identical passing status reads."""
    first = _statuses_for_head(
        host,
        head_sha,
        deadline_s=deadline_s,
        cancellation=cancellation,
    )
    if first is None:
        return None
    first_result = _status_snapshot(first, required_checks, head_sha)
    if first_result is None:
        return None
    second = _statuses_for_head(
        host,
        head_sha,
        deadline_s=deadline_s,
        cancellation=cancellation,
    )
    if second is None:
        return None
    second_result = _status_snapshot(second, required_checks, head_sha)
    if second_result is None or second_result[0] != first_result[0]:
        logger.warning("Commit statuses changed while reading %s", head_sha)
        return None
    return second_result[1]
