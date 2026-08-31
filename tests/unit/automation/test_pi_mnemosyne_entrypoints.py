"""Tests for pipeline scopes that require Athena/Mnemosyne preparation."""
# ruff: noqa: D103

from __future__ import annotations

from hephaestus.automation.pipeline.athena_executor_scope import pipeline_requires_athena_executor
from hephaestus.automation.pipeline.coordinator_types import PipelineConfig
from hephaestus.automation.pipeline.routing import PipelineScope, StageName


def _config(*, stages: frozenset[StageName] | None = None, **overrides: object) -> PipelineConfig:
    values: dict[str, object] = {
        "org": "HomericIntelligence",
        "repos": ["Hephaestus"],
    }
    if stages is not None:
        values["scope"] = PipelineScope(stages)
    values.update(overrides)
    return PipelineConfig(**values)  # type: ignore[arg-type]


def test_planning_and_implementation_advise_scopes_prepare_athena() -> None:
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.PLANNING})))
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.IMPLEMENTATION})))


def test_learn_scopes_prepare_athena_only_when_learn_enabled() -> None:
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.PLAN_REVIEW})))
    assert not pipeline_requires_athena_executor(
        _config(stages=frozenset({StageName.PLAN_REVIEW}), enable_learn=False)
    )
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.MERGE_WAIT})))


def test_dry_run_skips_host_but_every_live_scope_can_recover_learning() -> None:
    assert not pipeline_requires_athena_executor(_config(dry_run=True))
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.REPO})))
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.PR_REVIEW})))
    assert pipeline_requires_athena_executor(_config(stages=frozenset({StageName.FINISHED})))


def test_advise_disabled_scope_does_not_prepare_for_advise_only_stage() -> None:
    assert not pipeline_requires_athena_executor(
        _config(stages=frozenset({StageName.PLANNING}), no_advise=True, enable_learn=False)
    )
