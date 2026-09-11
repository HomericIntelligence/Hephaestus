"""Complete exact-head Check Suite and Check Run inventory reads."""

from __future__ import annotations

import json
import logging
import time
from threading import Event

from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_PAGE_SIZE = 100
_MAX_CHECK_SUITES = 1_000
_MAX_CHECK_RUNS = 2_000


def _nullable_app_id(record: dict[str, object]) -> int | None:
    """Return one explicit nullable App ID or reject malformed input."""
    if "app" not in record:
        raise ValueError("GitHub App field is missing")
    app = record["app"]
    if app is None:
        return None
    app_id = app.get("id") if isinstance(app, dict) else None
    if not isinstance(app_id, int) or isinstance(app_id, bool) or app_id <= 0:
        raise ValueError("GitHub App identity is malformed")
    return app_id


def _collection_page(payload: object, key: str) -> tuple[int, list[object]] | None:
    """Validate one bounded GitHub collection page."""
    if not isinstance(payload, dict):
        return None
    total_count = payload.get("total_count")
    items = payload.get(key)
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
        or not isinstance(items, list)
        or len(items) > _PAGE_SIZE
    ):
        return None
    return total_count, items


def _read_page(
    host: _PipelineGitHubHost,
    endpoint: str,
    *,
    deadline_s: float,
    cancellation: Event,
) -> object | None:
    """Read one GitHub page within the shared operation budget."""
    if cancellation.is_set():
        return None
    remaining = deadline_s - time.monotonic()
    if remaining <= 0:
        return None
    result = host._deadline_gh_call(
        ["api", endpoint],
        check=False,
        timeout=min(float(host._gh_timeout), remaining),
        deadline_s=deadline_s,
        shutdown=cancellation,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub returned an error for required checks")
    payload: object = json.loads(result.stdout or "null")
    return payload


def check_suite_ids_for_head(
    host: _PipelineGitHubHost,
    head_sha: str,
    *,
    deadline_s: float,
    cancellation: Event,
) -> tuple[tuple[int, int | None], ...] | None:
    """Return every validated Check Suite and nullable App ID for one commit."""
    owner, name = host._owner_name()
    endpoint = f"/repos/{owner}/{name}/commits/{head_sha}/check-suites?per_page={_PAGE_SIZE}"
    suite_inventory: list[tuple[int, int | None]] = []
    seen_ids: set[int] = set()
    expected_count: int | None = None
    page = 1
    while expected_count is None or len(suite_inventory) < expected_count:
        page_endpoint = endpoint if page == 1 else f"{endpoint}&page={page}"
        parsed = _collection_page(
            _read_page(
                host,
                page_endpoint,
                deadline_s=deadline_s,
                cancellation=cancellation,
            ),
            "check_suites",
        )
        if parsed is None:
            logger.warning("Check Suite page is malformed for %s", head_sha)
            return None
        total_count, suites = parsed
        if expected_count is None:
            expected_count = total_count
            if expected_count > _MAX_CHECK_SUITES:
                logger.warning("Check Suite inventory exceeds its safety ceiling for %s", head_sha)
                return None
        elif total_count != expected_count:
            logger.warning("Check Suite count changed for %s", head_sha)
            return None
        for suite in suites:
            if not isinstance(suite, dict):
                logger.warning("Check Suite page has invalid identity for %s", head_sha)
                return None
            suite_id = suite.get("id")
            try:
                suite_app_id = _nullable_app_id(suite)
            except ValueError:
                logger.warning("Check Suite page has invalid App identity for %s", head_sha)
                return None
            if (
                not isinstance(suite_id, int)
                or isinstance(suite_id, bool)
                or suite_id <= 0
                or suite_id in seen_ids
                or suite.get("head_sha") != head_sha
            ):
                logger.warning("Check Suite page has invalid identity for %s", head_sha)
                return None
            seen_ids.add(suite_id)
            suite_inventory.append((suite_id, suite_app_id))
        if len(suite_inventory) > expected_count or (
            not suites and len(suite_inventory) < expected_count
        ):
            logger.warning("Check Suite pages are incomplete for %s", head_sha)
            return None
        page += 1
    return tuple(sorted(suite_inventory))


def check_runs_for_head(
    host: _PipelineGitHubHost,
    head_sha: str,
    suite_inventory: tuple[tuple[int, int | None], ...],
    *,
    deadline_s: float,
    cancellation: Event,
) -> list[object] | None:
    """Return all Check Runs when the suite inventory proves completeness."""
    owner, name = host._owner_name()
    endpoint = (
        f"/repos/{owner}/{name}/commits/{head_sha}/check-runs?filter=all&per_page={_PAGE_SIZE}"
    )
    check_runs: list[object] = []
    check_run_ids: set[int] = set()
    known_suite_apps = dict(suite_inventory)
    expected_count: int | None = None
    page = 1
    while expected_count is None or len(check_runs) < expected_count:
        page_endpoint = endpoint if page == 1 else f"{endpoint}&page={page}"
        parsed = _collection_page(
            _read_page(
                host,
                page_endpoint,
                deadline_s=deadline_s,
                cancellation=cancellation,
            ),
            "check_runs",
        )
        if parsed is None:
            logger.warning("Check Run page is malformed for %s", head_sha)
            return None
        total_count, page_runs = parsed
        if expected_count is None:
            expected_count = total_count
            if expected_count > _MAX_CHECK_RUNS:
                logger.warning(
                    "Check Runs response exceeds the %d-run safety ceiling for %s",
                    _MAX_CHECK_RUNS,
                    head_sha,
                )
                return None
        elif total_count != expected_count:
            logger.warning("Check Run count changed for %s", head_sha)
            return None
        for check_run in page_runs:
            if not isinstance(check_run, dict):
                logger.warning("Check Run inventory has invalid identity for %s", head_sha)
                return None
            check_run_id = check_run.get("id")
            check_suite = check_run.get("check_suite")
            check_suite_id = check_suite.get("id") if isinstance(check_suite, dict) else None
            try:
                _nullable_app_id(check_run)
            except ValueError:
                logger.warning("Check Run inventory has invalid App identity for %s", head_sha)
                return None
            if (
                not isinstance(check_run_id, int)
                or isinstance(check_run_id, bool)
                or check_run_id <= 0
                or check_run_id in check_run_ids
                or check_run.get("head_sha") != head_sha
                or not isinstance(check_suite_id, int)
                or isinstance(check_suite_id, bool)
                or check_suite_id <= 0
                or check_suite_id not in known_suite_apps
            ):
                logger.warning("Check Run inventory has invalid identity for %s", head_sha)
                return None
            check_run_ids.add(check_run_id)
        check_runs.extend(page_runs)
        if len(check_runs) > expected_count or (not page_runs and len(check_runs) < expected_count):
            logger.warning("Check Run pages are incomplete for %s", head_sha)
            return None
        page += 1
    return check_runs
