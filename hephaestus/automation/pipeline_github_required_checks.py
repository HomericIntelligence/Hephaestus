"""Exact-head required status-evidence merge-gate queries."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from datetime import UTC, datetime
from threading import Event

import hephaestus.automation.github_api as github_api

from .pipeline_github_check_policy import EffectiveMergePolicy
from .pipeline_github_commit_statuses import (
    _current_evidence_timestamp,
    stable_passing_commit_status_requirements,
)
from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_CHECK_RUNS_PAGE_SIZE = 100
# Limit one exact-head traversal to 2,000 Check Runs.
_CHECK_RUNS_MAX_TOTAL_COUNT = 2_000
_CHECK_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_RequiredCheck = tuple[str, int | None]


def _status_evidence_now_utc() -> datetime:
    """Return the current UTC time through a test-controlled seam."""
    return datetime.now(UTC)


def _valid_app_id(value: object) -> int | None:
    """Return a valid nullable GitHub App ID."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError("GitHub App ID is malformed")


def _check_run_required_matches(
    check_run: dict[str, object],
    required_checks: frozenset[_RequiredCheck],
    head_sha: str,
) -> frozenset[_RequiredCheck] | None:
    """Return required entries matched by one Check Run, or ``None`` if malformed."""
    name = check_run.get("name")
    if not isinstance(name, str):
        logger.warning("Check Run for %s has no valid name", head_sha)
        return None
    named_requirements = {requirement for requirement in required_checks if requirement[0] == name}
    if not named_requirements:
        return frozenset()
    if all(requirement[1] is None for requirement in named_requirements):
        return frozenset(named_requirements)
    try:
        app_id = _check_run_app_id(check_run)
    except ValueError:
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    return frozenset(
        requirement
        for requirement in named_requirements
        if requirement[1] is None or requirement[1] == app_id
    )


def _check_run_app_id(check_run: dict[str, object]) -> int:
    """Return the positive GitHub App identity for one Check Run."""
    app = check_run.get("app")
    if not isinstance(app, dict):
        raise ValueError("Check Run has no application identity")
    app_id = _valid_app_id(app.get("id"))
    if app_id is None:
        raise ValueError("Check Run has no application identity")
    return app_id


def _check_run_page(payload: object, head_sha: str) -> tuple[int, list[object]] | None:
    """Validate one exact-head Check Runs response page."""
    if not isinstance(payload, dict):
        logger.warning("Check Runs response for %s is not an object", head_sha)
        return None
    total_count = payload.get("total_count")
    check_runs = payload.get("check_runs")
    if (
        not isinstance(total_count, int)
        or isinstance(total_count, bool)
        or total_count < 0
        or not isinstance(check_runs, list)
    ):
        logger.warning("Check Runs response for %s is malformed", head_sha)
        return None
    return total_count, check_runs


def _check_run_snapshot(
    check_runs: list[object],
    head_sha: str,
    required_checks: frozenset[_RequiredCheck],
    now_utc: datetime,
) -> tuple[object, ...] | None:
    """Return stable identity and status data for a Check Runs traversal."""
    snapshot: list[tuple[int, str, int, str, str, object, str, frozenset[_RequiredCheck]]] = []
    identities: set[tuple[str, int]] = set()
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            logger.warning("Check Run for %s is not an object", head_sha)
            return None
        try:
            app_id = _check_run_app_id(check_run)
        except ValueError:
            logger.warning("Check Run for %s has no valid app identity", head_sha)
            return None
        matches = _check_run_required_matches(check_run, required_checks, head_sha)
        if matches is None:
            return None
        if not matches:
            continue
        check_run_id = check_run.get("id")
        if not isinstance(check_run_id, int) or isinstance(check_run_id, bool) or check_run_id <= 0:
            logger.warning("Check Run for %s has no valid identity", head_sha)
            return None
        name = check_run.get("name")
        if not isinstance(name, str):
            logger.warning("Check Run for %s has no valid name", head_sha)
            return None
        identity = (name, app_id)
        if identity in identities:
            logger.warning("Check Runs contain duplicate context and app identity for %s", head_sha)
            return None
        identities.add(identity)
        completed_at = _current_evidence_timestamp(check_run.get("completed_at"), now_utc)
        if completed_at is None:
            logger.warning("Check Run for %s has no current completion time", head_sha)
            return None
        snapshot.append(
            (
                check_run_id,
                name,
                app_id,
                str(check_run.get("status") or "").lower(),
                str(check_run.get("conclusion") or "").lower(),
                check_run.get("head_sha"),
                completed_at,
                matches,
            )
        )
    return tuple(sorted(snapshot))


def _passing_check_run_requirements(
    check_runs: list[object],
    head_sha: str,
    required_checks: frozenset[_RequiredCheck],
    now_utc: datetime,
) -> frozenset[_RequiredCheck] | None:
    """Return requirements proved by passing exact-head Check Runs."""
    matched_checks: set[_RequiredCheck] = set()
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            return None
        try:
            _check_run_app_id(check_run)
        except ValueError:
            return None
        matches = _check_run_required_matches(check_run, required_checks, head_sha)
        if matches is None:
            return None
        if not matches:
            continue
        if check_run.get("head_sha") != head_sha:
            logger.warning("Check Run does not match reviewed head %s", head_sha)
            return None
        status = str(check_run.get("status") or "").lower()
        conclusion = str(check_run.get("conclusion") or "").lower()
        if (
            status != "completed"
            or conclusion not in _CHECK_SUCCESS_CONCLUSIONS
            or _current_evidence_timestamp(check_run.get("completed_at"), now_utc) is None
        ):
            return None
        matched_checks.update(matches)
    return frozenset(matched_checks)


class PipelineGitHubRequiredChecks(_PipelineGitHubHost):
    """Read exact-head required status evidence for the final merge gate."""

    def required_checks_pass_for_head(
        self,
        head_sha: str,
        policy: EffectiveMergePolicy,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> bool:
        """Return whether each effective requirement has passing exact-head evidence."""
        if (
            self._repo_slug is None
            or _FULL_COMMIT_SHA_RE.fullmatch(head_sha) is None
            or not isinstance(policy, EffectiveMergePolicy)
            or not isinstance(cancellation, Event)
        ):
            return False
        try:
            now_utc = _status_evidence_now_utc()
            if now_utc.tzinfo is None or now_utc.utcoffset() is None:
                raise ValueError("required status-evidence clock is not timezone-aware")
            now_utc = now_utc.astimezone(UTC)
            required_checks = frozenset(
                (check.context, check.app_id) for check in policy.required_checks
            )
            first = self._check_runs_for_head(
                head_sha, deadline_s=deadline_s, cancellation=cancellation
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
        ) as exc:
            logger.warning("Check Runs read failed for %s: %s", head_sha, exc)
            return False
        if not required_checks or first is None:
            return False
        first_snapshot = _check_run_snapshot(first, head_sha, required_checks, now_utc)
        if first_snapshot is None:
            return False
        try:
            second = self._check_runs_for_head(
                head_sha, deadline_s=deadline_s, cancellation=cancellation
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
        ) as exc:
            logger.warning("Check Runs stability read failed for %s: %s", head_sha, exc)
            return False
        if (
            second is None
            or _check_run_snapshot(second, head_sha, required_checks, now_utc) != first_snapshot
        ):
            logger.warning("Check Runs changed while reading %s", head_sha)
            return False
        run_requirements = _passing_check_run_requirements(
            second,
            head_sha,
            required_checks,
            now_utc,
        )
        if run_requirements is None:
            return False
        try:
            status_requirements = stable_passing_commit_status_requirements(
                self,
                head_sha,
                required_checks,
                deadline_s=deadline_s,
                cancellation=cancellation,
                now_utc=now_utc,
            )
        except (
            AttributeError,
            TypeError,
            ValueError,
            subprocess.SubprocessError,
            RuntimeError,
            OSError,
        ) as exc:
            logger.warning("Commit-status stability read failed for %s: %s", head_sha, exc)
            return False
        return (
            status_requirements is not None
            and (run_requirements | status_requirements) == required_checks
        )

    def _check_runs_for_head(
        self,
        head_sha: str,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> list[object] | None:
        """Read every Check Runs page for one exact commit."""
        owner, name = self._owner_name()
        endpoint = (
            f"/repos/{owner}/{name}/commits/{head_sha}/check-runs?filter=latest"
            f"&per_page={_CHECK_RUNS_PAGE_SIZE}"
        )
        check_runs: list[object] = []
        expected_count: int | None = None
        check_run_ids: set[int] = set()
        page = 1
        while expected_count is None or len(check_runs) < expected_count:
            if cancellation.is_set():
                return None
            remaining = deadline_s - time.monotonic()
            if remaining <= 0:
                return None
            page_endpoint = endpoint if page == 1 else f"{endpoint}&page={page}"
            result = github_api.gh_call(
                ["api", page_endpoint],
                check=False,
                timeout=min(float(self._gh_timeout), remaining),
            )
            if result.returncode != 0:
                raise RuntimeError("GitHub returned an error for Check Runs")
            payload = json.loads(result.stdout or "null")
            parsed = _check_run_page(payload, head_sha)
            if parsed is None:
                return None
            total_count, page_runs = parsed
            if expected_count is None:
                expected_count = total_count
                if expected_count > _CHECK_RUNS_MAX_TOTAL_COUNT:
                    logger.warning(
                        "Check Runs response exceeds the %d-run safety ceiling for %s",
                        _CHECK_RUNS_MAX_TOTAL_COUNT,
                        head_sha,
                    )
                    return None
            elif total_count != expected_count:
                logger.warning("Check Runs count changed for %s", head_sha)
                return None
            for check_run in page_runs:
                check_run_id = check_run.get("id") if isinstance(check_run, dict) else None
                if (
                    not isinstance(check_run_id, int)
                    or isinstance(check_run_id, bool)
                    or check_run_id <= 0
                    or check_run_id in check_run_ids
                ):
                    logger.warning("Check Runs page has invalid identity for %s", head_sha)
                    return None
                check_run_ids.add(check_run_id)
            check_runs.extend(page_runs)
            if len(check_runs) > expected_count or (
                not page_runs and len(check_runs) < expected_count
            ):
                logger.warning("Check Runs pages are incomplete for %s", head_sha)
                return None
            page += 1
        return check_runs
