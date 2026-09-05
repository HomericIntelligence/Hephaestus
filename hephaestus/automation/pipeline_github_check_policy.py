"""Stable effective branch-policy reads for the merge gate."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from fnmatch import fnmatchcase
from threading import Event
from typing import cast
from urllib.parse import quote

import hephaestus.automation.github_api as github_api

from .pipeline_github_contract import _PipelineGitHubHost

logger = logging.getLogger(__name__)

_RULESET_PAGE_SIZE = 100
_RULESET_MAX_TOTAL = 1_000
_BYPASS_STATES = frozenset({"never", "always", "pull_requests_only"})
_BYPASS_MODES = frozenset({"always", "pull_request", "exempt"})
_BYPASS_ACTOR_TYPES = frozenset(
    {"DeployKey", "Integration", "OrganizationAdmin", "RepositoryRole", "Team"}
)


@dataclass(frozen=True, order=True)
class RequiredCheck:
    """One required status context and its optional GitHub App identity."""

    context: str
    app_id: int | None


@dataclass(frozen=True)
class EffectiveMergePolicy:
    """Stable effective merge policy for one exact repository base branch."""

    base_branch: str
    required_checks: tuple[RequiredCheck, ...]
    conversation_resolution_enforced: bool
    bypassable_ruleset_ids: tuple[int, ...]


def _positive_app_id(value: object) -> int | None:
    """Return a valid nullable GitHub App ID."""
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError("GitHub App ID is malformed")


def _request(
    host: _PipelineGitHubHost,
    argv: list[str],
    *,
    deadline_s: float,
    cancellation: Event,
) -> subprocess.CompletedProcess[str]:
    """Run one GitHub read within the remaining aggregate operation budget."""
    if cancellation.is_set():
        raise TimeoutError("merge-policy read was cancelled")
    remaining = deadline_s - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("merge-policy read deadline expired")
    return github_api.gh_call(
        argv,
        check=False,
        timeout=min(float(host._gh_timeout), remaining),
    )


def _has_no_explicit_pull_request_bypasses(protection: dict[str, object]) -> bool:
    """Return whether classic protection grants no pull-request bypass."""
    reviews = protection.get("required_pull_request_reviews")
    if reviews is None:
        return True
    if not isinstance(reviews, dict):
        return False
    if "bypass_pull_request_allowances" not in reviews:
        return True
    allowances = reviews["bypass_pull_request_allowances"]
    if not isinstance(allowances, dict):
        return False
    for actor_type in ("users", "teams", "apps"):
        actors = allowances.get(actor_type)
        if not isinstance(actors, list) or actors:
            return False
    return True


def _classic_check_inventory(status_checks: object) -> set[RequiredCheck]:
    """Parse the classic required-status-check inventory."""
    checks: set[RequiredCheck] = set()
    if status_checks is None:
        return checks
    if not isinstance(status_checks, dict):
        raise ValueError("classic required status checks are malformed")
    contexts = status_checks.get("contexts")
    bound_checks = status_checks.get("checks")
    if not isinstance(contexts, list) or not isinstance(bound_checks, list):
        raise ValueError("classic required status checks are incomplete")
    if not all(isinstance(context, str) and context for context in contexts):
        raise ValueError("classic status-check context is malformed")
    context_names = list(contexts)
    if len(context_names) != len(set(context_names)):
        raise ValueError("classic status-check contexts contain duplicates")
    bound_names: list[str] = []
    for entry in bound_checks:
        if not isinstance(entry, dict):
            raise ValueError("classic status-check binding is malformed")
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            raise ValueError("classic status-check binding has no context")
        check = RequiredCheck(context, _positive_app_id(entry.get("app_id")))
        if check in checks:
            raise ValueError("classic status-check bindings contain duplicates")
        checks.add(check)
        bound_names.append(context)
    if bound_checks and set(context_names) != set(bound_names):
        raise ValueError("classic status-check inventories disagree")
    if not bound_checks:
        checks.update(RequiredCheck(context, None) for context in context_names)
    return checks


def _classic_policy(payload: object) -> tuple[set[RequiredCheck], bool]:
    """Parse classic protection checks and conversation enforcement."""
    if not isinstance(payload, dict):
        raise ValueError("classic branch protection is not an object")
    checks = _classic_check_inventory(payload.get("required_status_checks"))
    resolution = payload.get("required_conversation_resolution")
    admins = payload.get("enforce_admins")
    resolution_safe = bool(
        isinstance(resolution, dict)
        and resolution.get("enabled") is True
        and isinstance(admins, dict)
        and admins.get("enabled") is True
        and _has_no_explicit_pull_request_bypasses(payload)
    )
    return checks, resolution_safe


def _ruleset_applies(ruleset: dict[str, object], base_branch: str) -> bool:
    """Return whether one validated active branch ruleset applies to the base."""
    conditions = ruleset.get("conditions")
    if not isinstance(conditions, dict) or set(conditions) != {"ref_name"}:
        raise ValueError("ruleset branch conditions are malformed")
    ref_name = conditions["ref_name"]
    if not isinstance(ref_name, dict) or set(ref_name) != {"include", "exclude"}:
        raise ValueError("ruleset ref-name conditions are malformed")
    includes = ref_name["include"]
    excludes = ref_name["exclude"]
    if not isinstance(includes, list) or not isinstance(excludes, list):
        raise ValueError("ruleset ref-name patterns are malformed")
    if not all(isinstance(pattern, str) and pattern for pattern in [*includes, *excludes]):
        raise ValueError("ruleset ref-name pattern is malformed")
    ref = f"refs/heads/{base_branch}"

    def matches(pattern: str) -> bool:
        if pattern == "~DEFAULT_BRANCH":
            return True
        if pattern == "~ALL":
            return True
        return fnmatchcase(ref, pattern)

    return any(matches(pattern) for pattern in includes) and not any(
        matches(pattern) for pattern in excludes
    )


def _validate_bypass(ruleset: dict[str, object]) -> bool:
    """Validate bypass actors and return whether the live actor can bypass."""
    bypass = ruleset.get("current_user_can_bypass")
    actors = ruleset.get("bypass_actors")
    if bypass not in _BYPASS_STATES or not isinstance(actors, list):
        raise ValueError("ruleset bypass policy is malformed")
    for actor in actors:
        if not isinstance(actor, dict) or set(actor) != {
            "actor_id",
            "actor_type",
            "bypass_mode",
        }:
            raise ValueError("ruleset bypass actor is malformed")
        actor_id = actor["actor_id"]
        if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
            raise ValueError("ruleset bypass actor ID is malformed")
        if actor["actor_type"] not in _BYPASS_ACTOR_TYPES:
            raise ValueError("ruleset bypass actor type is unsupported")
        if actor["bypass_mode"] not in _BYPASS_MODES:
            raise ValueError("ruleset bypass mode is malformed")
    return bypass != "never"


def _required_checks_from_parameters(parameters: dict[str, object]) -> set[RequiredCheck]:
    """Parse one ruleset required-status-check parameter object."""
    required = parameters.get("required_status_checks")
    if not isinstance(required, list):
        raise ValueError("ruleset required status checks are malformed")
    checks: set[RequiredCheck] = set()
    for entry in required:
        if not isinstance(entry, dict):
            raise ValueError("ruleset required status-check binding is malformed")
        context = entry.get("context")
        if not isinstance(context, str) or not context:
            raise ValueError("ruleset required status-check context is malformed")
        check = RequiredCheck(context, _positive_app_id(entry.get("integration_id")))
        if check in checks:
            raise ValueError("ruleset required checks contain duplicates")
        checks.add(check)
    return checks


def _ruleset_rules(rules: object) -> tuple[set[RequiredCheck], bool]:
    """Parse required checks and thread resolution from a rules array."""
    if not isinstance(rules, list):
        raise ValueError("ruleset rules are malformed")
    checks: set[RequiredCheck] = set()
    requires_resolution = False
    seen_status_rule = False
    seen_pull_request_rule = False
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise ValueError("ruleset rule is malformed")
        rule_type = rule["type"]
        if rule_type not in {"required_status_checks", "pull_request"}:
            continue
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("ruleset rule parameters are malformed")
        if rule_type == "pull_request":
            if seen_pull_request_rule:
                raise ValueError("ruleset has duplicate pull-request rules")
            seen_pull_request_rule = True
            resolution = parameters.get("required_review_thread_resolution")
            if not isinstance(resolution, bool):
                raise ValueError("ruleset thread-resolution policy is malformed")
            requires_resolution = resolution
            continue
        if seen_status_rule:
            raise ValueError("ruleset has duplicate required-status-check rules")
        seen_status_rule = True
        checks.update(_required_checks_from_parameters(parameters))
    return checks, requires_resolution


def _ruleset_policy(
    ruleset: object,
    base_branch: str,
) -> tuple[set[RequiredCheck], bool, bool]:
    """Parse one active ruleset into checks, resolution, and live bypass facts."""
    if not isinstance(ruleset, dict):
        raise ValueError("ruleset detail is not an object")
    ruleset_id = ruleset.get("id")
    if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
        raise ValueError("ruleset ID is malformed")
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        raise ValueError("active branch ruleset identity is malformed")
    if not _ruleset_applies(ruleset, base_branch):
        return set(), False, False
    bypassable = _validate_bypass(ruleset)
    checks, requires_resolution = _ruleset_rules(ruleset.get("rules"))
    return checks, requires_resolution and not bypassable, bypassable


def _active_summary_id(summary: object, seen_ids: set[int]) -> int | None:
    """Validate one ruleset summary and return an active branch-ruleset ID."""
    if not isinstance(summary, dict):
        raise ValueError("repository ruleset summary is malformed")
    ruleset_id = summary.get("id")
    if (
        not isinstance(ruleset_id, int)
        or isinstance(ruleset_id, bool)
        or ruleset_id <= 0
        or ruleset_id in seen_ids
    ):
        raise ValueError("repository ruleset summary ID is malformed")
    seen_ids.add(ruleset_id)
    if summary.get("enforcement") == "disabled":
        return None
    if summary.get("enforcement") != "active" or summary.get("target") != "branch":
        raise ValueError("repository ruleset summary is malformed")
    return ruleset_id


def _validated_detail(detail: object, summary: dict[str, object]) -> dict[str, object]:
    """Return a ruleset detail only when it is bound to its list summary."""
    if not isinstance(detail, dict):
        raise ValueError("repository ruleset detail is malformed")
    for key in ("id", "name", "target", "source_type", "source", "enforcement"):
        if detail.get(key) != summary.get(key):
            raise ValueError("repository ruleset summary and detail disagree")
    return detail


class PipelineGitHubCheckPolicy(_PipelineGitHubHost):
    """Read one stable effective branch policy for a merge cycle."""

    def effective_merge_policy(
        self,
        pr_number: int,
        base_branch: str,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> EffectiveMergePolicy | None:
        """Return two identical complete policy snapshots, or fail closed."""
        if (
            pr_number <= 0
            or self._repo_slug is None
            or not isinstance(base_branch, str)
            or not base_branch
            or not isinstance(cancellation, Event)
        ):
            return None
        try:
            first = self._effective_merge_policy_once(
                base_branch, deadline_s=deadline_s, cancellation=cancellation
            )
            second = self._effective_merge_policy_once(
                base_branch, deadline_s=deadline_s, cancellation=cancellation
            )
        except (ValueError, TimeoutError, subprocess.SubprocessError, RuntimeError, OSError) as exc:
            logger.warning("PR #%s: effective merge-policy read failed: %s", pr_number, exc)
            return None
        if first != second:
            logger.warning("PR #%s: effective merge policy changed while reading", pr_number)
            return None
        return second

    def _effective_merge_policy_once(
        self,
        base_branch: str,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> EffectiveMergePolicy:
        """Read one complete classic-and-ruleset policy snapshot."""
        owner, name = self._owner_name()
        branch = quote(base_branch, safe="")
        classic_result = _request(
            self,
            ["api", "--method", "GET", f"/repos/{owner}/{name}/branches/{branch}/protection"],
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
        if classic_result.returncode != 0:
            raise RuntimeError("GitHub returned an error for classic branch protection")
        classic_checks, classic_resolution = _classic_policy(
            json.loads(classic_result.stdout or "null")
        )
        details = self._active_rulesets(
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
        checks = set(classic_checks)
        ruleset_resolution = False
        bypassable: list[int] = []
        for detail in details:
            ruleset_checks, safe_resolution, can_bypass = _ruleset_policy(detail, base_branch)
            checks.update(ruleset_checks)
            ruleset_resolution = ruleset_resolution or safe_resolution
            if can_bypass and _ruleset_applies(detail, base_branch):
                bypassable.append(cast(int, detail["id"]))
        return EffectiveMergePolicy(
            base_branch=base_branch,
            required_checks=tuple(sorted(checks)),
            conversation_resolution_enforced=classic_resolution or ruleset_resolution,
            bypassable_ruleset_ids=tuple(sorted(bypassable)),
        )

    def _active_rulesets(
        self,
        *,
        deadline_s: float,
        cancellation: Event,
    ) -> tuple[dict[str, object], ...]:
        """Return all repository active branch-ruleset details."""
        owner, name = self._owner_name()
        summaries: list[object] = []
        page = 1
        while True:
            endpoint = (
                f"/repos/{owner}/{name}/rulesets?includes_parents=true&targets=branch"
                f"&per_page={_RULESET_PAGE_SIZE}&page={page}"
            )
            result = _request(
                self,
                ["api", "--method", "GET", endpoint],
                deadline_s=deadline_s,
                cancellation=cancellation,
            )
            if result.returncode != 0:
                raise RuntimeError("GitHub returned an error for repository rulesets")
            payload = json.loads(result.stdout or "null")
            if not isinstance(payload, list):
                raise ValueError("repository ruleset response is not a list")
            summaries.extend(payload)
            if len(summaries) > _RULESET_MAX_TOTAL:
                raise ValueError("repository ruleset response exceeds the safety limit")
            if len(payload) < _RULESET_PAGE_SIZE:
                break
            page += 1
        details: list[dict[str, object]] = []
        seen_ids: set[int] = set()
        for summary in summaries:
            ruleset_id = _active_summary_id(summary, seen_ids)
            if ruleset_id is None:
                continue
            summary_dict = cast(dict[str, object], summary)
            result = _request(
                self,
                ["api", "--method", "GET", f"/repos/{owner}/{name}/rulesets/{ruleset_id}"],
                deadline_s=deadline_s,
                cancellation=cancellation,
            )
            if result.returncode != 0:
                raise RuntimeError("GitHub returned an error for repository ruleset detail")
            details.append(_validated_detail(json.loads(result.stdout or "null"), summary_dict))
        return tuple(details)
