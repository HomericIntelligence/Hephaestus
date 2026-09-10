"""Expose the opt-in queue automation product through lazy imports.

Install ``HomericIntelligence-Hephaestus[automation]`` to use this product.
The base library does not import this package. See ADR-0001 for the boundary.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hephaestus.automation.dependency_resolver import DependencyResolver
    from hephaestus.automation.models import IssueInfo
    from hephaestus.automation.pipeline.coordinator import run_pipeline
    from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
    from hephaestus.automation.pipeline.routing import PipelineScope, StageName

__all__ = [
    "DependencyResolver",
    "IssueInfo",
    "PipelineConfig",
    "PipelineScope",
    "StageName",
    "run_pipeline",
]

_LAZY_EXPORTS: dict[str, str] = {
    "DependencyResolver": "hephaestus.automation.dependency_resolver",
    "IssueInfo": "hephaestus.automation.models",
    "PipelineConfig": "hephaestus.automation.pipeline.coordinator_types",
    "PipelineScope": "hephaestus.automation.pipeline.routing",
    "StageName": "hephaestus.automation.pipeline.routing",
    "run_pipeline": "hephaestus.automation.pipeline.coordinator",
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
