"""Stable effective branch-policy reads for the merge gate."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from threading import Event
from typing import cast
from urllib.parse import quote

from .pipeline_github_contract import _PipelineGitHubHost
from .pipeline_github_merge_rules import (
    RequiredCheck as RequiredCheck,
    required_check_sort_key,
    ruleset_rule_facts,
)
from .pipeline_github_ruleset_conditions import required_app_id, ruleset_applies
from .pipeline_github_transport import _parse_included_http_response

logger = logging.getLogger(__name__)

_RULESET_PAGE_SIZE = 100
_RULESET_MAX_TOTAL = 1_000
_BYPASS_STATES = frozenset({"never", "always", "pull_requests_only"})
_BYPASS_MODES = frozenset({"always", "pull_request", "exempt"})
_IGNORED_BYPASS_ACTOR_ID_TYPES = frozenset({"EnterpriseOwner", "OrganizationAdmin"})
_BYPASS_ACTOR_TYPES = frozenset(
    {
        "DeployKey",
        "EnterpriseOwner",
        "EnterpriseRole",
        "Integration",
        "OrganizationAdmin",
        "RepositoryRole",
        "Team",
        "User",
    }
)


@dataclass(frozen=True)
class EffectiveMergePolicy:
    """Stable effective merge policy for one exact repository base branch.

    ``strict_update_enforced`` is true only when at least one applicable policy
    source enforces strict updates for the current actor.
    """

    base_branch: str
    default_branch: str
    required_checks: tuple[RequiredCheck, ...]
    conversation_resolution_enforced: bool
    bypassable_ruleset_ids: tuple[int, ...]
    strict_update_enforced: bool = False
    merge_queue_method: str | None = None

    @property
    def merge_queue_required(self) -> bool:
        """Return whether server policy requires the merge queue."""
        return self.merge_queue_method is not None


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
    return host._deadline_gh_call(
        argv,
        check=False,
        timeout=min(float(host._gh_timeout), remaining),
        deadline_s=deadline_s,
        shutdown=cancellation,
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
    bound_checks = status_checks.get("checks", [])
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
        check = RequiredCheck(context, required_app_id(entry.get("app_id")))
        if check in checks:
            raise ValueError("classic status-check bindings contain duplicates")
        checks.add(check)
        bound_names.append(context)
    if bound_checks and set(context_names) != set(bound_names):
        raise ValueError("classic status-check inventories disagree")
    if not bound_checks:
        checks.update(RequiredCheck(context, None) for context in context_names)
    return checks


def _classic_policy(payload: object) -> tuple[set[RequiredCheck], bool, bool]:
    """Parse classic checks, conversation enforcement, and strict update."""
    if not isinstance(payload, dict):
        raise ValueError("classic branch protection is not an object")
    status_checks = payload.get("required_status_checks")
    checks = _classic_check_inventory(status_checks)
    admins = payload.get("enforce_admins")
    admins_enforced = bool(isinstance(admins, dict) and admins.get("enabled") is True)
    strict_update = False
    if status_checks is not None:
        if not isinstance(status_checks, dict) or not isinstance(status_checks.get("strict"), bool):
            raise ValueError("classic required status-check strictness is malformed")
        strict_update = status_checks["strict"] and admins_enforced
    resolution = payload.get("required_conversation_resolution")
    resolution_safe = bool(
        isinstance(resolution, dict)
        and resolution.get("enabled") is True
        and admins_enforced
        and _has_no_explicit_pull_request_bypasses(payload)
    )
    return checks, resolution_safe, strict_update


def _classic_policy_response(
    result: subprocess.CompletedProcess[str],
) -> tuple[set[RequiredCheck], bool, bool]:
    """Parse classic protection or one exact absent-protection response."""
    stdout = result.stdout if isinstance(result.stdout, str) else ""
    status, body, malformed = _parse_included_http_response(stdout)
    if result.returncode == 0:
        if status is None:
            return _classic_policy(json.loads(stdout or "null"))
        if status == 200 and not malformed:
            return _classic_policy(body)
        raise RuntimeError("GitHub returned a malformed classic branch-protection response")
    if (
        status == 404
        and not malformed
        and isinstance(body, dict)
        and body.get("message") == "Branch not protected"
    ):
        return set(), False, False
    raise RuntimeError("GitHub returned an error for classic branch protection")


def _validate_bypass(ruleset: dict[str, object]) -> bool:
    """Validate bypass actors and return whether the live actor can bypass."""
    bypass = ruleset.get("current_user_can_bypass")
    actors = ruleset.get("bypass_actors")
    if not isinstance(bypass, str) or bypass not in _BYPASS_STATES or not isinstance(actors, list):
        raise ValueError("ruleset bypass policy is malformed")
    for actor in actors:
        if not isinstance(actor, dict) or set(actor) != {
            "actor_id",
            "actor_type",
            "bypass_mode",
        }:
            raise ValueError("ruleset bypass actor is malformed")
        actor_type = actor["actor_type"]
        bypass_mode = actor["bypass_mode"]
        if not isinstance(actor_type, str) or actor_type not in _BYPASS_ACTOR_TYPES:
            raise ValueError("ruleset bypass actor type is unsupported")
        if not isinstance(bypass_mode, str) or bypass_mode not in _BYPASS_MODES:
            raise ValueError("ruleset bypass mode is malformed")
        actor_id = actor["actor_id"]
        if actor_type == "DeployKey":
            if actor_id is not None or bypass_mode == "pull_request":
                raise ValueError("ruleset bypass actor is malformed")
        elif actor_type in _IGNORED_BYPASS_ACTOR_ID_TYPES:
            if actor_id is not None and (
                not isinstance(actor_id, int) or isinstance(actor_id, bool)
            ):
                raise ValueError("ruleset bypass actor ID is malformed")
        elif not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
            raise ValueError("ruleset bypass actor ID is malformed")
    return bypass != "never"


def _ruleset_policy(
    ruleset: object,
    base_branch: str,
    default_branch: str,
) -> tuple[set[RequiredCheck], bool, bool, bool, str | None]:
    """Parse one active ruleset into actor-bound policy facts."""
    if not isinstance(ruleset, dict):
        raise ValueError("ruleset detail is not an object")
    ruleset_id = ruleset.get("id")
    if not isinstance(ruleset_id, int) or isinstance(ruleset_id, bool) or ruleset_id <= 0:
        raise ValueError("ruleset ID is malformed")
    if ruleset.get("target") != "branch" or ruleset.get("enforcement") != "active":
        raise ValueError("active branch ruleset identity is malformed")
    if not ruleset_applies(ruleset, base_branch, default_branch):
        return set(), False, False, False, None
    bypassable = _validate_bypass(ruleset)
    checks, requires_resolution, strict_update, merge_queue_method = ruleset_rule_facts(
        ruleset.get("rules")
    )
    return (
        checks,
        requires_resolution and not bypassable,
        bypassable,
        strict_update and not bypassable,
        merge_queue_method,
    )


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
    if summary.get("target") != "branch":
        raise ValueError("repository ruleset summary is malformed")
    enforcement = summary.get("enforcement")
    if enforcement in {"disabled", "evaluate"}:
        return None
    if enforcement != "active":
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
        repository_result = _request(
            self,
            ["api", "--method", "GET", f"/repos/{owner}/{name}"],
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
        if repository_result.returncode != 0:
            raise RuntimeError("GitHub returned an error for repository metadata")
        repository = json.loads(repository_result.stdout or "null")
        default_branch = repository.get("default_branch") if isinstance(repository, dict) else None
        if not isinstance(default_branch, str) or not default_branch:
            raise ValueError("repository default branch is malformed")
        branch = quote(base_branch, safe="")
        classic_result = _request(
            self,
            [
                "api",
                "--method",
                "GET",
                "--include",
                f"/repos/{owner}/{name}/branches/{branch}/protection",
            ],
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
        classic_checks, classic_resolution, classic_strict = _classic_policy_response(
            classic_result
        )
        details = self._active_rulesets(
            deadline_s=deadline_s,
            cancellation=cancellation,
        )
        checks = set(classic_checks)
        ruleset_resolution = False
        bypassable: list[int] = []
        strict_update = classic_strict
        merge_queue_method: str | None = None
        for detail in details:
            (
                ruleset_checks,
                safe_resolution,
                can_bypass,
                ruleset_strict,
                ruleset_queue_method,
            ) = _ruleset_policy(detail, base_branch, default_branch)
            checks.update(ruleset_checks)
            ruleset_resolution = ruleset_resolution or safe_resolution
            if can_bypass:
                bypassable.append(cast(int, detail["id"]))
            strict_update = strict_update or ruleset_strict
            if ruleset_queue_method is not None:
                if merge_queue_method not in {None, ruleset_queue_method}:
                    raise ValueError("applicable merge-queue methods disagree")
                merge_queue_method = ruleset_queue_method
        return EffectiveMergePolicy(
            base_branch=base_branch,
            default_branch=default_branch,
            required_checks=tuple(sorted(checks, key=required_check_sort_key)),
            conversation_resolution_enforced=classic_resolution or ruleset_resolution,
            bypassable_ruleset_ids=tuple(sorted(bypassable)),
            strict_update_enforced=strict_update,
            merge_queue_method=merge_queue_method,
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
