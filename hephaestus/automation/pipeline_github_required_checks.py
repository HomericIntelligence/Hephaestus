"""Exact-head required Check Runs merge-gate queries."""

from __future__ import annotations

import json
import logging
import re
import subprocess

from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_FULL_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_CHECK_RUNS_PAGE_SIZE = 100
_CHECK_RUNS_MAX_TOTAL_COUNT = 2_000
_RULES_PAGE_SIZE = 100
_RULES_MAX_TOTAL_COUNT = 2_000
_CHECK_SUCCESS_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_ANY_APP_ID = -1
_RequiredCheck = tuple[str, int | None]


def _valid_app_id(value: object, *, allow_wildcard: bool = False) -> int | None:
    """Return a valid GitHub App ID, or return ``None``."""
    if value is None:
        return None
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and (value > 0 or (allow_wildcard and value == _ANY_APP_ID))
    ):
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
    app = check_run.get("app")
    if not isinstance(app, dict):
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    try:
        app_id = _valid_app_id(app.get("id"))
    except ValueError:
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    if app_id is None:
        logger.warning("Check Run for %s has no valid app identity", head_sha)
        return None
    return frozenset(
        requirement
        for requirement in named_requirements
        if requirement[1] in (None, _ANY_APP_ID) or requirement[1] == app_id
    )


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
) -> tuple[object, ...] | None:
    """Return stable identity and status data for a Check Runs traversal."""
    snapshot: list[tuple[int, str, str, str, object, frozenset[_RequiredCheck]]] = []
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            logger.warning("Check Run for %s is not an object", head_sha)
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
        snapshot.append(
            (
                check_run_id,
                name,
                str(check_run.get("status") or "").lower(),
                str(check_run.get("conclusion") or "").lower(),
                check_run.get("head_sha"),
                matches,
            )
        )
    return tuple(sorted(snapshot))


def _required_check_runs_pass(
    check_runs: list[object],
    head_sha: str,
    required_checks: frozenset[_RequiredCheck],
) -> bool:
    """Return whether all required Check Runs match and succeed on ``head_sha``."""
    saw_success = False
    matched_checks: set[_RequiredCheck] = set()
    for check_run in check_runs:
        if not isinstance(check_run, dict):
            return False
        matches = _check_run_required_matches(check_run, required_checks, head_sha)
        if matches is None:
            return False
        if not matches:
            continue
        if check_run.get("head_sha") != head_sha:
            logger.warning("Check Run does not match reviewed head %s", head_sha)
            return False
        status = str(check_run.get("status") or "").lower()
        conclusion = str(check_run.get("conclusion") or "").lower()
        if status != "completed" or conclusion not in _CHECK_SUCCESS_CONCLUSIONS:
            return False
        matched_checks.update(matches)
        saw_success = saw_success or conclusion == "success"
    return matched_checks == required_checks and saw_success


class PipelineGitHubRequiredChecks(_PipelineGitHubHost):
    """Read exact-head required Check Runs for the final merge gate."""

    def required_checks_pass_for_head(self, head_sha: str) -> bool:
        """Return whether every effective required Check Run succeeds for ``head_sha``."""
        if self._repo_slug is None or _FULL_COMMIT_SHA_RE.fullmatch(head_sha) is None:
            return False
        try:
            required_checks = self._required_checks_for_main()
            first = self._check_runs_for_head(head_sha)
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
        if not required_checks or first is None or not first:
            return False
        if len(first) > _CHECK_RUNS_PAGE_SIZE:
            first_snapshot = _check_run_snapshot(first, head_sha, required_checks)
            if first_snapshot is None:
                return False
            try:
                second = self._check_runs_for_head(head_sha)
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
                or _check_run_snapshot(second, head_sha, required_checks) != first_snapshot
            ):
                logger.warning("Check Runs changed while reading %s", head_sha)
                return False
            checks = second
        else:
            checks = first
        return _required_check_runs_pass(checks, head_sha, required_checks)

    def _required_checks_for_main(self) -> frozenset[_RequiredCheck] | None:
        """Read and union classic and effective branch requirements for ``main``."""
        owner, name = self._owner_name()
        classic_endpoint = f"/repos/{owner}/{name}/branches/main/protection/required_status_checks"
        rules_endpoint = f"/repos/{owner}/{name}/rules/branches/main"
        classic = self._gh(["api", "--method", "GET", classic_endpoint], check=False)
        if classic.returncode != 0:
            raise RuntimeError("GitHub returned an error for required status checks")
        rules = self._rules_for_main(rules_endpoint)
        classic_checks = _classic_required_checks(classic.stdout or "null")
        return classic_checks | _ruleset_required_checks(json.dumps(rules))

    def _rules_for_main(self, endpoint: str) -> list[object]:
        """Read one complete and stable active-rules snapshot for ``main``."""
        first = self._rules_for_main_traversal(endpoint)
        if len(first) < _RULES_PAGE_SIZE:
            return first
        second = self._rules_for_main_traversal(endpoint)
        if second != first:
            raise RuntimeError("GitHub active rules changed while reading main")
        return second

    def _rules_for_main_traversal(self, endpoint: str) -> list[object]:
        """Read every active-rules page for ``main`` or raise."""
        rules: list[object] = []
        page_fingerprints: set[str] = set()
        page = 1
        while True:
            page_endpoint = f"{endpoint}?per_page={_RULES_PAGE_SIZE}&page={page}"
            result = self._gh(["api", "--method", "GET", page_endpoint], check=False)
            if result.returncode != 0:
                raise RuntimeError("GitHub returned an error for active rules")
            payload = json.loads(result.stdout or "null")
            if not isinstance(payload, list) or len(payload) > _RULES_PAGE_SIZE:
                raise ValueError("Active rules response for main has a malformed page")
            fingerprint = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            if fingerprint in page_fingerprints:
                raise RuntimeError("GitHub active-rules pagination repeated a page")
            page_fingerprints.add(fingerprint)
            rules.extend(payload)
            if len(rules) > _RULES_MAX_TOTAL_COUNT:
                raise RuntimeError("GitHub active rules exceed the safety ceiling")
            if len(payload) < _RULES_PAGE_SIZE:
                return rules
            page += 1

    def _check_runs_for_head(self, head_sha: str) -> list[object] | None:
        """Read every Check Runs page for one exact commit."""
        owner, name = self._owner_name()
        endpoint = (
            f"/repos/{owner}/{name}/commits/{head_sha}/check-runs?per_page={_CHECK_RUNS_PAGE_SIZE}"
        )
        check_runs: list[object] = []
        expected_count: int | None = None
        check_run_ids: set[int] = set()
        page = 1
        while expected_count is None or len(check_runs) < expected_count:
            page_endpoint = endpoint if page == 1 else f"{endpoint}&page={page}"
            result = self._gh(["api", page_endpoint], check=False)
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


def _classic_required_checks(payload_text: str) -> frozenset[_RequiredCheck]:
    """Parse classic branch-protection status-check requirements."""
    payload = json.loads(payload_text)
    if not isinstance(payload, dict):
        raise ValueError("Required status checks response for main is not an object")
    contexts = payload.get("contexts")
    checks = payload.get("checks", [])
    if not isinstance(contexts, list) or not isinstance(checks, list):
        raise ValueError("Required status checks response for main is malformed")
    required_checks: set[_RequiredCheck] = set()
    for context in contexts:
        if not isinstance(context, str) or not context:
            raise ValueError("Required status checks response for main is malformed")
        required_checks.add((context, None))
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Required status checks response for main is malformed")
        context = check.get("context")
        try:
            app_id = _valid_app_id(check.get("app_id"), allow_wildcard=True)
        except ValueError:
            raise ValueError("Required status checks response for main is malformed") from None
        if not isinstance(context, str) or not context:
            raise ValueError("Required status checks response for main is malformed")
        required_checks.add((context, app_id))
    return frozenset(required_checks)


def _ruleset_required_checks(payload_text: str) -> frozenset[_RequiredCheck]:
    """Parse active-rules required status-check requirements."""
    payload = json.loads(payload_text)
    if not isinstance(payload, list):
        raise ValueError("Active rules response for main is not a list")
    required_checks: set[_RequiredCheck] = set()
    for rule in payload:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise ValueError("Active rules response for main is malformed")
        if rule["type"] != "required_status_checks":
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("Active rules response for main is malformed")
        checks = parameters.get("required_status_checks")
        if not isinstance(checks, list):
            raise ValueError("Active rules response for main is malformed")
        for check in checks:
            if not isinstance(check, dict):
                raise ValueError("Active rules response for main is malformed")
            context = check.get("context")
            try:
                app_id = _valid_app_id(check.get("integration_id"), allow_wildcard=True)
            except ValueError:
                raise ValueError("Active rules response for main is malformed") from None
            if not isinstance(context, str) or not context:
                raise ValueError("Active rules response for main is malformed")
            required_checks.add((context, app_id))
    return frozenset(required_checks)
