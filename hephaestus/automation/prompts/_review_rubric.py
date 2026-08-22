"""Review-rubric accessors backed by external Jinja templates."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from .catalog import PromptCatalog

_DEFAULT_PLUGIN_SKILLS_SUBPATH = Path(".claude/plugins/marketplaces/Hephaestus/skills")
_PR_REVIEW_SKILL_NAME = "pr-review"
_PLUGIN_SKILLS_DIR: ContextVar[Path | None] = ContextVar("plugin_skills_dir", default=None)


@contextmanager
def plugin_skills_context(skills_dir: Path | None) -> Iterator[None]:
    """Set the per-invocation skill root and reliably restore prior state."""
    token = _PLUGIN_SKILLS_DIR.set(skills_dir)
    try:
        yield
    finally:
        _PLUGIN_SKILLS_DIR.reset(token)


def _skill_reference(skills_dir: Path | None = None) -> str:
    """Resolve the installed Athena pr-review skill when available."""
    skills_dir = (
        skills_dir or _PLUGIN_SKILLS_DIR.get() or Path.home() / _DEFAULT_PLUGIN_SKILLS_SUBPATH
    )
    candidate = skills_dir / _PR_REVIEW_SKILL_NAME / "SKILL.md"
    return str(candidate) if candidate.is_file() else ""


def build_review_rubric(skills_dir: Path | None = None) -> str:
    """Render the review rubric with its runtime skill reference."""
    skill_ref = _skill_reference(skills_dir)
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


def get_plan_review_output_format() -> str:
    """Return the exact state-label contract used by plan reviewers."""
    return _fragment("plan_review_output_format")


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
    return f"{build_review_rubric()}\n\n{_fragment('pr')}"


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
    "get_plan_review_output_format",
    "get_plan_review_rubric",
    "get_pr_review_rubric",
    "get_review_output_format",
    "plugin_skills_context",
]
