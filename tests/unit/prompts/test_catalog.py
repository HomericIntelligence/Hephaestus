"""Behavioral contracts for packaged Jinja prompt templates."""

from __future__ import annotations

import hashlib
from pathlib import Path

from hephaestus.automation._review_utils import build_automation_parser
from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.automation.prompts.planning import (
    get_plan_prompt,
)


def test_default_catalog_render_preserves_planning_compatibility() -> None:
    """The packaged default preserves the established rendered planning prompt."""
    rendered = get_plan_prompt(99, catalog=PromptCatalog())

    assert hashlib.sha256(rendered.encode()).hexdigest() == (
        "fc771ef42130ab2a67b73b38902690611dc433c2588d73d42820201da2a05061"
    )


def test_harness_template_replaces_only_the_matching_default(tmp_path: Path) -> None:
    """A harness may replace one named template without copying the default tree."""
    override = tmp_path / "planning" / "plan.j2"
    override.parent.mkdir()
    override.write_text("Harness plan for issue {{ issue_number }}\n", encoding="utf-8")

    catalog = PromptCatalog(override_root=tmp_path)

    assert get_plan_prompt(42, catalog=catalog) == "Harness plan for issue 42\n"


def test_prompt_dir_cli_flag_selects_the_optional_override(tmp_path: Path) -> None:
    """Only an explicit optional command-line flag activates an override."""
    template = tmp_path / "planning" / "plan.j2"
    template.parent.mkdir()
    template.write_text("CLI {{ issue_number }}\n")

    parser = build_automation_parser("test parser")
    try:
        parser.parse_args(["--prompt-dir", str(tmp_path)])
        assert get_plan_prompt(42) == "CLI 42\n"
    finally:
        PromptCatalog.clear_current()


def test_default_catalog_has_no_implicit_override() -> None:
    """Only an optional CLI-selected catalog may enable an override."""
    PromptCatalog.clear_current()

    assert get_plan_prompt(12).startswith("\nCreate an implementation plan")


def test_harness_can_override_a_shared_prompt_fragment(tmp_path: Path) -> None:
    """A shared fragment override applies inside an otherwise default prompt."""
    fragment = tmp_path / "shared" / "terse_output_directive.j2"
    fragment.parent.mkdir()
    fragment.write_text("HARNESS DIRECTIVE", encoding="utf-8")

    rendered = get_plan_prompt(5, catalog=PromptCatalog(override_root=tmp_path))

    assert "HARNESS DIRECTIVE" in rendered
    assert "Output discipline (token budget)" not in rendered
