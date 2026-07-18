"""Review-rubric accessors backed by external Jinja templates."""

from __future__ import annotations

import os
from pathlib import Path

from .catalog import PromptCatalog

_DEFAULT_PLUGIN_SKILLS_SUBPATH = Path(".claude/plugins/marketplaces/Hephaestus/skills")
_PR_REVIEW_SKILL_NAME = "pr-review"


def _skill_reference() -> str:
    """Resolve the installed Athena pr-review skill when available."""
    override = os.environ.get("HEPHAESTUS_PLUGIN_SKILLS_DIR")
    skills_dir = Path(override) if override else Path.home() / _DEFAULT_PLUGIN_SKILLS_SUBPATH
    candidate = skills_dir / _PR_REVIEW_SKILL_NAME / "SKILL.md"
    return str(candidate) if candidate.is_file() else ""


def build_review_rubric() -> str:
    """Render the review rubric with its runtime skill reference."""
    skill_ref = _skill_reference()
    if skill_ref:
        skill_line = (
            "`$athena:pr-review` skill (rubric summarized below — refer to the full skill at\n"
            f"`{skill_ref}`\nif available):"
        )
    else:
        skill_line = "`$athena:pr-review` skill (rubric summarized below):"
    return PromptCatalog.current().render("review_rubrics/reviewer.j2", skill_line=skill_line)


def _fragment(name: str) -> str:
    """Render one static rubric fragment through the active catalog."""
    return PromptCatalog.current().render(f"review_rubrics/{name}.j2")


def get_review_output_format() -> str:
    """Return the review verdict-format fragment."""
    return _fragment("review_output_format")


def get_plan_review_rubric() -> str:
    """Return the plan-review rubric."""
    return _fragment("plan")


def get_plan_loop_review_rubric() -> str:
    """Return the iterative plan-review rubric."""
    return _fragment("plan_loop")


def get_implementation_loop_review_rubric() -> str:
    """Return the iterative implementation-review rubric."""
    return _fragment("implementation_loop")


def get_pr_review_rubric() -> str:
    """Return the PR-review rubric."""
    return _fragment("pr")


def get_full_sweep_suffix() -> str:
    """Return the final review sweep fragment."""
    return _fragment("full_sweep")


# Compatibility values retain historical imports while production prompt
# builders use the accessors above so --prompt-dir overlays every fragment.
_REVIEW_RUBRIC = build_review_rubric()
_REVIEW_OUTPUT_FORMAT = get_review_output_format()
_PR_REVIEW_RUBRIC_DIMENSIONS = _fragment("pr_dimensions")
_REVIEW_GRADING_AND_ANTI_INFLATION = _fragment("grading")
_SEVEN_PRINCIPLES_DIMENSIONS = _fragment("principles")
_PLAN_REVIEW_RUBRIC = get_plan_review_rubric()
_PLAN_LOOP_REVIEW_RUBRIC = get_plan_loop_review_rubric()
_IMPL_LOOP_REVIEW_RUBRIC = get_implementation_loop_review_rubric()
_PR_REVIEW_RUBRIC = get_pr_review_rubric()
_FULL_SWEEP_SUFFIX = get_full_sweep_suffix()

__all__ = [
    "_FULL_SWEEP_SUFFIX",
    "_IMPL_LOOP_REVIEW_RUBRIC",
    "_PLAN_LOOP_REVIEW_RUBRIC",
    "_PLAN_REVIEW_RUBRIC",
    "_PR_REVIEW_RUBRIC",
    "_PR_REVIEW_RUBRIC_DIMENSIONS",
    "_REVIEW_GRADING_AND_ANTI_INFLATION",
    "_REVIEW_OUTPUT_FORMAT",
    "_REVIEW_RUBRIC",
    "_SEVEN_PRINCIPLES_DIMENSIONS",
    "build_review_rubric",
    "get_full_sweep_suffix",
    "get_implementation_loop_review_rubric",
    "get_plan_loop_review_rubric",
    "get_plan_review_rubric",
    "get_pr_review_rubric",
    "get_review_output_format",
]
