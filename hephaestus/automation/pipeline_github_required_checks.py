"""Exact-head required status-evidence merge-gate queries."""

from __future__ import annotations

import logging
import re
import subprocess
from datetime import UTC, datetime
from threading import Event

from .pipeline_github_check_policy import EffectiveMergePolicy
from .pipeline_github_check_run_inventory import (
    check_runs_for_suites,
    check_suite_ids_for_head,
)
from .pipeline_github_commit_statuses import (
    _current_evidence_timestamp,
    stable_passing_commit_status_requirements,
)
from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_GITHUB_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)"
)
_CHECK_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_CHECK_CONCLUSIONS = _CHECK_SUCCESS_CONCLUSIONS | frozenset(
    {"action_required", "cancelled", "failure", "stale", "timed_out"}
)
_RequiredCheck = tuple[str, int | None]
_CheckRunCandidate = tuple[datetime, int, dict[str, object]]
_CheckRunGroups = dict[_RequiredCheck, dict[int, list[_CheckRunCandidate]]]


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
) -> tuple[int, int, str, str, str, datetime] | None:
    """Return validated identity and terminal evidence for one matching run."""
    try:
        app_id = _check_run_app_id(check_run)
    except ValueError:
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    check_run_id = check_run.get("id")
    status = check_run.get("status")
    conclusion = check_run.get("conclusion")
    completion_time = _check_run_completion_time(check_run.get("completed_at"))
    if (
        not isinstance(check_run_id, int)
        or isinstance(check_run_id, bool)
        or check_run_id <= 0
        or not isinstance(status, str)
        or status != "completed"
        or not isinstance(conclusion, str)
        or conclusion not in _CHECK_CONCLUSIONS
        or completion_time is None
        or check_run.get("head_sha") != head_sha
    ):
        logger.warning("Check Run for %s has malformed terminal evidence", head_sha)
        return None
    raw_completed_at, completed_at = completion_time
    return check_run_id, app_id, status, conclusion, raw_completed_at, completed_at


def _check_run_snapshot(
    check_runs: list[object],
    head_sha: str,
    required_checks: frozenset[_RequiredCheck],
) -> tuple[object, ...] | None:
    """Return stable identity and status data for a Check Runs traversal."""
    snapshot: list[tuple[int, str, int, str, str, object, str, frozenset[_RequiredCheck]]] = []
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            logger.warning("Check Run for %s is not an object", head_sha)
            return None
        matches = _check_run_required_matches(check_run, required_checks, head_sha)
        if matches is None:
            return None
        if not matches:
            continue
        validated = _validated_check_run(check_run, head_sha)
        if validated is None:
            return None
        check_run_id, app_id, status, conclusion, completed_at, _completed_at_utc = validated
        name = check_run.get("name")
        if not isinstance(name, str):
            logger.warning("Check Run for %s has no valid name", head_sha)
            return None
        snapshot.append(
            (
                check_run_id,
                name,
                app_id,
                status,
                conclusion,
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
    candidate_runs: _CheckRunGroups = {}
    seen_ids: set[int] = set()
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            return None
        matches = _check_run_required_matches(check_run, required_checks, head_sha)
        if matches is None:
            return None
        if not matches:
            continue
        validated = _validated_check_run(check_run, head_sha)
        if validated is None:
            return None
        check_run_id, app_id, _status, _conclusion, _raw_completed_at, completed_at = validated
        if check_run_id in seen_ids:
            logger.warning("Check Run for %s has no unambiguous identity", head_sha)
            return None
        seen_ids.add(check_run_id)
        for requirement in matches:
            runs_by_app = candidate_runs.setdefault(requirement, {})
            runs_by_app.setdefault(app_id, []).append((completed_at, check_run_id, check_run))

    return _passing_current_check_runs(candidate_runs, now_utc)


def _unique_current_check_run(
    candidates: list[_CheckRunCandidate], context: str
) -> dict[str, object] | None:
    """Return the only run at the maximum completion instant."""
    maximum_completed_at = max(completed_at for completed_at, _run_id, _run in candidates)
    current = [candidate for candidate in candidates if candidate[0] == maximum_completed_at]
    if len(current) != 1:
        logger.warning("Required Check Run context %s has equal current completion times", context)
        return None
    return current[0][2]


def _passing_current_check_runs(
    current_runs: _CheckRunGroups,
    now_utc: datetime,
) -> frozenset[_RequiredCheck] | None:
    """Return passing requirements from unambiguous current Check Runs."""
    matched_checks: set[_RequiredCheck] = set()
    for requirement, runs_by_app in current_runs.items():
        if requirement[1] is None and len(runs_by_app) != 1:
            logger.warning(
                "Required Check Run context %s has multiple application identities",
                requirement[0],
            )
            return None
        check_run = _unique_current_check_run(next(iter(runs_by_app.values())), requirement[0])
        if check_run is None:
            return None
        conclusion = check_run.get("conclusion")
        if (
            conclusion not in _CHECK_SUCCESS_CONCLUSIONS
            or _current_evidence_timestamp(check_run.get("completed_at"), now_utc) is None
        ):
            return None
        matched_checks.add(requirement)
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
            suite_ids = self._check_suite_ids_for_head(
                head_sha, deadline_s=deadline_s, cancellation=cancellation
            )
            if suite_ids is None:
                return False
            first = self._check_runs_for_head(
                head_sha,
                suite_ids,
                deadline_s=deadline_s,
                cancellation=cancellation,
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
        first_snapshot = _check_run_snapshot(first, head_sha, required_checks)
        if first_snapshot is None:
            return False
        try:
            second = self._check_runs_for_head(
                head_sha,
                suite_ids,
                deadline_s=deadline_s,
                cancellation=cancellation,
            )
            final_suite_ids = self._check_suite_ids_for_head(
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
            or final_suite_ids != suite_ids
            or _check_run_snapshot(second, head_sha, required_checks) != first_snapshot
        ):
            logger.warning("Check Suite or Check Run inventory changed for %s", head_sha)
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
        suite_ids: tuple[int, ...],
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> list[object] | None:
        """Read all Check Runs for the validated exact-head suites."""
        return check_runs_for_suites(
            self,
            suite_ids,
            head_sha,
            deadline_s=deadline_s,
            cancellation=cancellation,
        )

    def _check_suite_ids_for_head(
        self,
        head_sha: str,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> tuple[int, ...] | None:
        """Read all Check Suite identities for one exact commit."""
        return check_suite_ids_for_head(
            self,
            head_sha,
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
