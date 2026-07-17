"""Strict-review rubric accessors backed by external Jinja templates."""

from __future__ import annotations

import os
from pathlib import Path

from .catalog import PromptCatalog

_DEFAULT_PLUGIN_SKILLS_SUBPATH = Path(".claude/plugins/marketplaces/Hephaestus/skills")
_STRICT_SKILL_NAME = "review-pr-strict"


def _skill_reference() -> str:
    """Resolve the optional installed strict-review skill reference."""
    override = os.environ.get("HEPHAESTUS_PLUGIN_SKILLS_DIR")
    skills_dir = Path(override) if override else Path.home() / _DEFAULT_PLUGIN_SKILLS_SUBPATH
    candidate = skills_dir / _STRICT_SKILL_NAME / "SKILL.md"
    return str(candidate) if candidate.is_file() else ""


def build_strict_review_rubric() -> str:
    """Render the strict-review rubric with its runtime skill reference."""
    skill_ref = _skill_reference()
    if skill_ref:
        skill_line = (
            "`review-pr-strict` skill (rubric summarized below — refer to the full skill at\n"
            f"`{skill_ref}`\nif available):"
        )
    else:
        skill_line = "`review-pr-strict` skill (rubric summarized below):"
    return PromptCatalog.current().render("strict_rubrics/reviewer.j2", skill_line=skill_line)


def _fragment(name: str) -> str:
    """Render one static rubric fragment through the active catalog."""
    return PromptCatalog.current().render(f"strict_rubrics/{name}.j2")


def get_strict_review_output_format() -> str:
    return _fragment("review_output_format")


def get_plan_strict_rubric() -> str:
    return _fragment("plan")


def get_plan_loop_strict_rubric() -> str:
    return _fragment("plan_loop")


def get_implementation_loop_strict_rubric() -> str:
    return _fragment("implementation_loop")


def get_pr_strict_rubric() -> str:
    return _fragment("pr")


def get_full_sweep_suffix() -> str:
    return _fragment("full_sweep")


# Compatibility values retain historical imports while production prompt
# builders use the accessors above so --prompt-dir overlays every fragment.
_STRICT_REVIEW_RUBRIC = build_strict_review_rubric()
_STRICT_REVIEW_OUTPUT_FORMAT = get_strict_review_output_format()
_PR_STRICT_RUBRIC_DIMENSIONS = _fragment("pr_dimensions")
_STRICT_GRADING_AND_ANTI_INFLATION = _fragment("grading")
_SEVEN_PRINCIPLES_DIMENSIONS = _fragment("principles")
_PLAN_STRICT_RUBRIC = get_plan_strict_rubric()
_PLAN_LOOP_STRICT_RUBRIC = get_plan_loop_strict_rubric()
_IMPL_LOOP_STRICT_RUBRIC = get_implementation_loop_strict_rubric()
_PR_STRICT_RUBRIC = get_pr_strict_rubric()
_FULL_SWEEP_SUFFIX = get_full_sweep_suffix()

__all__ = [
    "_FULL_SWEEP_SUFFIX",
    "_IMPL_LOOP_STRICT_RUBRIC",
    "_PLAN_LOOP_STRICT_RUBRIC",
    "_PLAN_STRICT_RUBRIC",
    "_PR_STRICT_RUBRIC",
    "_PR_STRICT_RUBRIC_DIMENSIONS",
    "_SEVEN_PRINCIPLES_DIMENSIONS",
    "_STRICT_GRADING_AND_ANTI_INFLATION",
    "_STRICT_REVIEW_OUTPUT_FORMAT",
    "_STRICT_REVIEW_RUBRIC",
    "build_strict_review_rubric",
    "get_full_sweep_suffix",
    "get_implementation_loop_strict_rubric",
    "get_plan_loop_strict_rubric",
    "get_plan_strict_rubric",
    "get_pr_strict_rubric",
    "get_strict_review_output_format",
]
