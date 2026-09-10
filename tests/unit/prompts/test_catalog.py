"""Behavioral contracts for packaged Jinja prompt templates."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.prompts.planning import (
    get_plan_prompt,
)
from hephaestus.prompts import PromptCatalog

WRITING_STANDARD_SENTINEL = "ASD-STE100 Simplified Technical English, Issue 9"


def test_default_catalog_render_exposes_planning_contract() -> None:
    """The packaged default renders the planning prompt's stable contracts."""
    rendered = get_plan_prompt(99, catalog=PromptCatalog())

    # Interpolation is observable without pinning unrelated wording or whitespace.
    assert "Create an implementation plan for GitHub issue #99." in rendered
    assert "{{ issue_number }}" not in rendered

    # The worked examples remain properly fenced so their commands are unambiguous.
    assert "```bash" in rendered
    assert rendered.count("```") % 2 == 0

    # The planner must return the complete markdown artifact, not a status summary.
    assert "Your FINAL message must BE the complete plan, as markdown" in rendered
    assert "starting with the\n`# Implementation Plan` heading" in rendered
    assert "Do NOT post a status note, changelog, or" in rendered


def test_default_templates_resolve_by_filesystem_path_not_package_metadata() -> None:
    """The default loader is ``__file__``-relative, immune to the rebuild race (#2308).

    ``PackageLoader`` consults importlib package metadata, which is transiently
    inconsistent right after an editable-install rebuild and crashed reviewer
    workers. The default loader must resolve templates by a filesystem path
    that exists on disk, independent of installed metadata.
    """
    from hephaestus.prompts.catalog import _DEFAULT_TEMPLATES_DIR

    assert _DEFAULT_TEMPLATES_DIR.is_dir()
    assert (_DEFAULT_TEMPLATES_DIR / "pr_review" / "analysis.j2").is_file()
    assert (_DEFAULT_TEMPLATES_DIR / "pr_review" / "analysis_opencode.j2").is_file()
    # The catalog loads its templates from that path (no PackageLoader involved).
    names = PromptCatalog()._environment.list_templates()
    assert "pr_review/analysis.j2" in names
    assert "pr_review/analysis_opencode.j2" in names


def test_harness_template_replaces_only_the_matching_default(tmp_path: Path) -> None:
    """A harness may replace one named template without copying the default tree."""
    override = tmp_path / "planning" / "plan.j2"
    override.parent.mkdir()
    override.write_text("Harness plan for issue {{ issue_number }}\n", encoding="utf-8")

    catalog = PromptCatalog(override_root=tmp_path)

    rendered = get_plan_prompt(42, catalog=catalog)

    assert WRITING_STANDARD_SENTINEL in rendered
    assert "Harness plan for issue 42\n" in rendered


def test_prompt_dir_cli_flag_selects_the_optional_override(tmp_path: Path) -> None:
    """Only an explicit optional command-line flag activates an override."""
    template = tmp_path / "planning" / "plan.j2"
    template.parent.mkdir()
    template.write_text("CLI {{ issue_number }}\n")

    parser = build_automation_parser("test parser")
    try:
        parser.parse_args(["--prompt-dir", str(tmp_path)])
        rendered = get_plan_prompt(42)
        assert WRITING_STANDARD_SENTINEL in rendered
        assert rendered.endswith("CLI 42\n")
    finally:
        PromptCatalog.clear_current()


def test_default_catalog_has_no_implicit_override() -> None:
    """Only an optional CLI-selected catalog may enable an override."""
    PromptCatalog.clear_current()

    rendered = get_plan_prompt(12)
    assert rendered.startswith("## Writing standard\n\n")
    assert "\nCreate an implementation plan" in rendered


def test_environment_does_not_select_a_prompt_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """Harness overrides are deliberately CLI-only, never environment-selected."""
    monkeypatch.setenv("HEPHAESTUS_PROMPT_DIR", "/does/not/exist")
    PromptCatalog.clear_current()

    rendered = get_plan_prompt(12)
    assert rendered.startswith("## Writing standard\n\n")
    assert "\nCreate an implementation plan" in rendered


def test_later_cli_parse_without_prompt_dir_resets_prior_override(tmp_path: Path) -> None:
    """An in-process CLI invocation cannot leak its overlay into the next one."""
    template = tmp_path / "planning" / "plan.j2"
    template.parent.mkdir()
    template.write_text("CLI {{ issue_number }}\n", encoding="utf-8")

    parser = build_automation_parser("test parser")
    parser.parse_args(["--prompt-dir", str(tmp_path)])
    rendered = get_plan_prompt(42)
    assert WRITING_STANDARD_SENTINEL in rendered
    assert rendered.endswith("CLI 42\n")

    parser.parse_args([])

    rendered = get_plan_prompt(42)
    assert rendered.startswith("## Writing standard\n\n")
    assert "\nCreate an implementation plan" in rendered


def test_harness_can_override_a_shared_prompt_fragment(tmp_path: Path) -> None:
    """A shared fragment override applies inside an otherwise default prompt."""
    fragment = tmp_path / "shared" / "terse_output_directive.j2"
    fragment.parent.mkdir()
    fragment.write_text("HARNESS DIRECTIVE", encoding="utf-8")

    rendered = get_plan_prompt(5, catalog=PromptCatalog(override_root=tmp_path))

    assert "HARNESS DIRECTIVE" in rendered
    assert "Output discipline (token budget)" not in rendered
    assert WRITING_STANDARD_SENTINEL in rendered


def test_review_fix_prompt_inherits_model_selection() -> None:
    """Difficulty labels must not select a different model."""
    from hephaestus.automation.prompts.address_review import get_address_review_prompt

    rendered = get_address_review_prompt(1, 2, "/work", "[]")
    assert "same tool and\n   model as this implementation session" in rendered
    assert "Model tier by difficulty" not in rendered
    for name in ("haiku", "sonnet", "opus", "fable"):
        assert f"`{name}`" not in rendered


@pytest.mark.parametrize(
    "name",
    [
        "ADDRESS_REVIEW_PROMPT",
        "ADVISE_PROMPT",
        "CODEX_ADVISE_PROMPT",
        "DIRTY_REUSED_WORKTREE_DECISION_PROMPT",
        "DIRTY_REUSED_WORKTREE_PROMPT",
        "IMPLEMENTATION_PROMPT",
        "IMPL_LOOP_REVIEW_PROMPT",
        "IMPL_RESUME_FEEDBACK_PROMPT",
        "PLAN_LOOP_REVIEW_PROMPT",
        "PLAN_PROMPT",
        "PLAN_REVIEW_PROMPT",
        "PR_REVIEW_ANALYSIS_PROMPT",
        "get_advise_prompt",
        "get_codex_advise_prompt",
        "get_advise_prompt_builder",
    ],
)
def test_retired_prompt_exports_are_unavailable(name: str) -> None:
    """Removed prompt interfaces cannot dispatch through a compatibility bridge."""
    from hephaestus.automation import prompts

    assert name not in prompts.__all__
    with pytest.raises(AttributeError):
        getattr(prompts, name)


@pytest.mark.parametrize("module", ["catalog", "advise"])
def test_retired_automation_prompt_modules_cannot_be_imported(module: str) -> None:
    """The catalog has one library owner and retired builders stay removed."""
    qualified_name = f"hephaestus.automation.prompts.{module}"
    with pytest.raises(ModuleNotFoundError) as error:
        importlib.import_module(qualified_name)
    assert error.value.name == qualified_name


def test_catalog_does_not_expose_the_removed_string_template_reader() -> None:
    """Callers render a template through the current catalog API."""
    removed_name = "source"
    with pytest.raises(AttributeError):
        getattr(PromptCatalog(), removed_name)
