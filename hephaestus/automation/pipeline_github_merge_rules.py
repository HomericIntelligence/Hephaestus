"""Typed rule facts for the effective GitHub merge policy."""

from __future__ import annotations

from dataclasses import dataclass

from .pipeline_github_ruleset_conditions import required_app_id


@dataclass(frozen=True)
class RequiredCheck:
    """One required status context and its optional GitHub App identity."""

    context: str
    app_id: int | None


def required_check_sort_key(check: RequiredCheck) -> tuple[str, int, int]:
    """Return a total order for unbound and app-bound check identities."""
    return (check.context, check.app_id is not None, check.app_id or 0)


def _required_checks(parameters: dict[str, object]) -> set[RequiredCheck]:
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
        check = RequiredCheck(context, required_app_id(entry.get("integration_id")))
        if check in checks:
            raise ValueError("ruleset required checks contain duplicates")
        checks.add(check)
    return checks


def _required_status_rule(
    parameters: dict[str, object],
) -> tuple[set[RequiredCheck], bool]:
    """Parse one complete ruleset status-check rule."""
    strict = parameters.get("strict_required_status_checks_policy")
    if not isinstance(strict, bool):
        raise ValueError("ruleset required status-check strictness is malformed")
    return _required_checks(parameters), strict


def ruleset_rule_facts(rules: object) -> tuple[set[RequiredCheck], bool, bool, str | None]:
    """Parse checks, thread resolution, strict update, and queue mode."""
    if not isinstance(rules, list):
        raise ValueError("ruleset rules are malformed")
    checks: set[RequiredCheck] = set()
    requires_resolution = False
    seen: set[str] = set()
    strict_update = False
    merge_queue_method: str | None = None
    selected = {"required_status_checks", "pull_request", "merge_queue"}
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("type"), str):
            raise ValueError("ruleset rule is malformed")
        rule_type = rule["type"]
        if rule_type not in selected:
            continue
        if rule_type in seen:
            raise ValueError(f"ruleset has duplicate {rule_type} rules")
        seen.add(rule_type)
        parameters = rule.get("parameters")
        if not isinstance(parameters, dict):
            raise ValueError("ruleset rule parameters are malformed")
        if rule_type == "pull_request":
            resolution = parameters.get("required_review_thread_resolution")
            if not isinstance(resolution, bool):
                raise ValueError("ruleset thread-resolution policy is malformed")
            requires_resolution = resolution
        elif rule_type == "merge_queue":
            method = parameters.get("merge_method")
            if method not in {"MERGE", "SQUASH", "REBASE"}:
                raise ValueError("ruleset merge-queue method is malformed")
            merge_queue_method = str(method)
        else:
            status_checks, strict_update = _required_status_rule(parameters)
            checks.update(status_checks)
    return checks, requires_resolution, strict_update, merge_queue_method
