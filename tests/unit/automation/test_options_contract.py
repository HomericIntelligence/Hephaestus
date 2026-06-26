"""Contract tests for automation option model inheritance."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel

import hephaestus.automation.models as automation_models
from hephaestus.automation import (
    address_review,
    ci_driver,
    implementer_cli,
    plan_reviewer,
    planner,
    pr_reviewer,
)
from hephaestus.automation.models import (
    AddressReviewOptions,
    CIDriverOptions,
    ImplementerOptions,
    PlannerOptions,
    PlanReviewerOptions,
    ReviewerOptions,
)

OPTION_CASES: tuple[tuple[type[BaseModel], dict[str, Any], dict[str, Any]], ...] = (
    (PlannerOptions, {"issues": [1]}, {"dry_run": True, "parallel": 7}),
    (ImplementerOptions, {}, {"dry_run": True, "max_workers": 7}),
    (ReviewerOptions, {}, {"dry_run": True, "max_workers": 7}),
    (PlanReviewerOptions, {}, {"dry_run": True, "max_workers": 7, "verbose": True}),
    (AddressReviewOptions, {}, {"dry_run": True, "max_workers": 7, "verbose": True}),
    (CIDriverOptions, {}, {"dry_run": True, "max_workers": 7, "verbose": True}),
)

OPTION_FIELD_CASES: tuple[tuple[type[BaseModel], frozenset[str]], ...] = (
    (
        PlannerOptions,
        frozenset(
            {
                "issues",
                "issues_explicit",
                "agent",
                "dry_run",
                "force",
                "parallel",
                "system_prompt_file",
                "skip_closed",
                "enable_advise",
            }
        ),
    ),
    (
        ImplementerOptions,
        frozenset(
            {
                "epic_number",
                "issues",
                "agent",
                "analyze_only",
                "health_check",
                "resume",
                "max_workers",
                "skip_closed",
                "auto_merge",
                "dry_run",
                "enable_advise",
                "enable_learn",
                "enable_follow_up",
                "enable_ui",
                "run_pre_pr_tests",
                "include_nitpicks",
            }
        ),
    ),
    (
        ReviewerOptions,
        frozenset({"issues", "agent", "max_workers", "dry_run", "enable_learn", "enable_ui"}),
    ),
    (
        PlanReviewerOptions,
        frozenset({"issues", "agent", "max_workers", "dry_run", "enable_ui", "verbose"}),
    ),
    (
        AddressReviewOptions,
        frozenset(
            {
                "issues",
                "agent",
                "max_workers",
                "dry_run",
                "enable_ui",
                "verbose",
                "resume_impl_session",
            }
        ),
    ),
    (
        CIDriverOptions,
        frozenset(
            {
                "issues",
                "prs",
                "agent",
                "max_workers",
                "dry_run",
                "enable_advise",
                "enable_learn",
                "enable_ui",
                "verbose",
                "max_fix_iterations",
                "force_merge_on_stall",
                "include_bot_prs",
                "include_all_authors",
                "enable_mechanical_rebase",
            }
        ),
    ),
)


@pytest.mark.parametrize(("options_cls", "required", "overrides"), OPTION_CASES)
def test_shared_worker_fields_preserve_defaults_and_overrides(
    options_cls: type[BaseModel],
    required: dict[str, Any],
    overrides: dict[str, Any],
) -> None:
    """Shared worker fields preserve canonical defaults and keyword overrides."""
    default_options = options_cls(**required)
    model_values = default_options.model_dump()
    worker_count = getattr(automation_models, "DEFAULT_WORKER_COUNT", object())

    if "parallel" in options_cls.model_fields:
        assert model_values["parallel"] == worker_count
    if "max_workers" in options_cls.model_fields:
        assert model_values["max_workers"] == worker_count
    assert model_values["dry_run"] is False
    if "verbose" in options_cls.model_fields:
        assert model_values["verbose"] is False

    custom_options = options_cls(**required, **overrides)
    for field_name, expected in overrides.items():
        assert getattr(custom_options, field_name) == expected


@pytest.mark.parametrize(("options_cls", "expected_fields"), OPTION_FIELD_CASES)
def test_option_public_model_signature_and_schema_contract(
    options_cls: type[BaseModel],
    expected_fields: frozenset[str],
) -> None:
    """Option models preserve public field names across Pydantic surfaces."""
    assert frozenset(options_cls.model_fields) == expected_fields
    assert frozenset(inspect.signature(options_cls).parameters) == expected_fields
    assert frozenset(options_cls.model_json_schema()["properties"]) == expected_fields


def test_worker_option_base_classes_hold_canonical_shared_defaults() -> None:
    """Base classes expose the canonical shared option defaults."""
    assert hasattr(automation_models, "WorkerOptionsBase")
    assert hasattr(automation_models, "ParallelWorkerOptionsBase")
    assert hasattr(automation_models, "VerboseParallelWorkerOptionsBase")
    assert hasattr(automation_models, "DEFAULT_WORKER_COUNT")

    assert automation_models.WorkerOptionsBase.model_fields["dry_run"].default is False
    assert (
        automation_models.ParallelWorkerOptionsBase.model_fields["max_workers"].default
        == automation_models.DEFAULT_WORKER_COUNT
    )
    verbose_default = automation_models.VerboseParallelWorkerOptionsBase.model_fields[
        "verbose"
    ].default
    assert verbose_default is False


def test_worker_option_classes_use_narrowest_base_class() -> None:
    """Option models inherit only the shared fields they actually expose."""
    assert hasattr(automation_models, "WorkerOptionsBase")
    assert hasattr(automation_models, "ParallelWorkerOptionsBase")
    assert hasattr(automation_models, "VerboseParallelWorkerOptionsBase")

    assert issubclass(PlannerOptions, automation_models.WorkerOptionsBase)
    assert not issubclass(PlannerOptions, automation_models.ParallelWorkerOptionsBase)
    assert issubclass(ImplementerOptions, automation_models.ParallelWorkerOptionsBase)
    assert not issubclass(ImplementerOptions, automation_models.VerboseParallelWorkerOptionsBase)
    assert issubclass(ReviewerOptions, automation_models.ParallelWorkerOptionsBase)
    assert not issubclass(ReviewerOptions, automation_models.VerboseParallelWorkerOptionsBase)
    assert issubclass(PlanReviewerOptions, automation_models.VerboseParallelWorkerOptionsBase)
    assert issubclass(AddressReviewOptions, automation_models.VerboseParallelWorkerOptionsBase)
    assert issubclass(CIDriverOptions, automation_models.VerboseParallelWorkerOptionsBase)


@pytest.mark.parametrize(
    ("build_parser", "ordered_flags"),
    (
        (planner._build_parser, ("--dry-run", "--force", "--parallel")),
        (
            implementer_cli._build_parser,
            ("--resume", "--max-workers", "--no-skip-closed", "--dry-run"),
        ),
        (pr_reviewer._build_parser, ("--max-workers", "--dry-run", "--no-ui", "--verbose")),
        (plan_reviewer._build_parser, ("--max-workers", "--dry-run", "--no-ui", "--verbose")),
        (address_review._build_parser, ("--max-workers", "--dry-run", "--no-ui", "--verbose")),
        (
            ci_driver._build_parser,
            ("--max-workers", "--dry-run", "--no-ui", "--no-advise", "--verbose"),
        ),
    ),
)
def test_worker_cli_help_order_is_stable(
    build_parser: Callable[[], Any],
    ordered_flags: tuple[str, ...],
) -> None:
    """CLI help keeps worker flags in the existing user-facing order."""
    help_text = build_parser().format_help()
    positions = [help_text.index(flag) for flag in ordered_flags]
    assert positions == sorted(positions)
