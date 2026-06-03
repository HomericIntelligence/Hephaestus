"""Automation utilities for GitHub issue planning and implementation."""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hephaestus.automation.dependency_resolver import DependencyResolver
    from hephaestus.automation.implementer import IssueImplementer
    from hephaestus.automation.models import (
        ImplementerOptions,
        IssueInfo,
        PlannerOptions,
        ReviewerOptions,
    )
    from hephaestus.automation.planner import Planner
    from hephaestus.automation.pr_reviewer import PRReviewer

__all__ = [
    "DependencyResolver",
    "ImplementerOptions",
    "IssueImplementer",
    "IssueInfo",
    "PRReviewer",
    "Planner",
    "PlannerOptions",
    "ReviewerOptions",
]

_LAZY_EXPORTS: dict[str, str] = {
    "DependencyResolver": "hephaestus.automation.dependency_resolver",
    "ImplementerOptions": "hephaestus.automation.models",
    "IssueImplementer": "hephaestus.automation.implementer",
    "IssueInfo": "hephaestus.automation.models",
    "Planner": "hephaestus.automation.planner",
    "PlannerOptions": "hephaestus.automation.models",
    "PRReviewer": "hephaestus.automation.pr_reviewer",
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
