"""Automation product layer for the HomericIntelligence ecosystem.

This subpackage is the Claude/Codex automation pipeline (Planner,
Implementer, CIDriver, reviewers, loop runner, curses TUI). It is the
product layer of ProjectHephaestus; the rest of `hephaestus.*` is a
utility library. Install with
``pip install HomericIntelligence-Hephaestus[automation]`` to opt in.

See ``docs/adr/0001-automation-library-boundary.md`` for the boundary
contract. `import hephaestus` does not load this subpackage.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hephaestus.automation.address_review import AddressReviewer
    from hephaestus.automation.audit_reviewer import AuditReviewer
    from hephaestus.automation.ci_driver import CIDriver
    from hephaestus.automation.dependency_resolver import DependencyResolver
    from hephaestus.automation.implementer import IssueImplementer
    from hephaestus.automation.models import (
        AddressReviewOptions,
        CIDriverOptions,
        ImplementerOptions,
        IssueInfo,
        PlannerOptions,
        PlanReviewerOptions,
        ReviewerOptions,
    )
    from hephaestus.automation.plan_reviewer import PlanReviewer
    from hephaestus.automation.planner import Planner
    from hephaestus.automation.pr_reviewer import PRReviewer

__all__ = [
    "AddressReviewOptions",
    "AddressReviewer",
    "AuditReviewer",
    "CIDriver",
    "CIDriverOptions",
    "DependencyResolver",
    "ImplementerOptions",
    "IssueImplementer",
    "IssueInfo",
    "PRReviewer",
    "PlanReviewer",
    "PlanReviewerOptions",
    "Planner",
    "PlannerOptions",
    "ReviewerOptions",
]

_LAZY_EXPORTS: dict[str, str] = {
    "AddressReviewOptions": "hephaestus.automation.models",
    "AddressReviewer": "hephaestus.automation.address_review",
    "AuditReviewer": "hephaestus.automation.audit_reviewer",
    "CIDriver": "hephaestus.automation.ci_driver",
    "CIDriverOptions": "hephaestus.automation.models",
    "DependencyResolver": "hephaestus.automation.dependency_resolver",
    "ImplementerOptions": "hephaestus.automation.models",
    "IssueImplementer": "hephaestus.automation.implementer",
    "IssueInfo": "hephaestus.automation.models",
    "PRReviewer": "hephaestus.automation.pr_reviewer",
    "PlanReviewer": "hephaestus.automation.plan_reviewer",
    "PlanReviewerOptions": "hephaestus.automation.models",
    "Planner": "hephaestus.automation.planner",
    "PlannerOptions": "hephaestus.automation.models",
    "ReviewerOptions": "hephaestus.automation.models",
}


def __getattr__(name: str) -> Any:
    """Load package-level exports without preloading phase entrypoints."""
    try:
        module_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include lazy exports in interactive introspection."""
    return sorted(set(globals()) | set(__all__))
