"""Behavioral contracts for packaged Jinja prompt templates."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.automation.prompts._shared import _TERSE_OUTPUT_DIRECTIVE
from hephaestus.automation.prompts.catalog import PromptCatalog
from hephaestus.automation.prompts.planning import PLAN_PROMPT, get_plan_prompt


def test_default_catalog_render_matches_legacy_plan_prompt() -> None:
    """The packaged default must preserve the current instantiated prompt exactly."""
    catalog = PromptCatalog()

    expected = PLAN_PROMPT.format(
        issue_number=99,
        terse_output_directive=_TERSE_OUTPUT_DIRECTIVE,
    )

    assert get_plan_prompt(99, catalog=catalog) == expected


def test_harness_template_replaces_only_the_matching_default(tmp_path: Path) -> None:
    """A harness may replace one named template without copying the default tree."""
    override = tmp_path / "planning" / "plan.j2"
    override.parent.mkdir()
    override.write_text("Harness plan for issue {{ issue_number }}\n", encoding="utf-8")

    catalog = PromptCatalog(override_root=tmp_path)

    assert get_plan_prompt(42, catalog=catalog) == "Harness plan for issue 42\n"


def test_explicit_override_root_wins_over_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The command-line value must take precedence over harness environment."""
    env_root = tmp_path / "env"
    explicit_root = tmp_path / "explicit"
    for root, text in ((env_root, "environment"), (explicit_root, "explicit")):
        template = root / "planning" / "plan.j2"
        template.parent.mkdir(parents=True)
        template.write_text(f"{text} {{{{ issue_number }}}}\n")
    monkeypatch.setenv("HEPHAESTUS_PROMPT_DIR", str(env_root))

    catalog = PromptCatalog.from_environment(override_root=explicit_root)

    assert get_plan_prompt(7, catalog=catalog) == "explicit 7\n"


def test_environment_override_applies_without_catalog_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Existing builder callers honor the harness environment by default."""
    template = tmp_path / "planning" / "plan.j2"
    template.parent.mkdir()
    template.write_text("environment {{ issue_number }}\n")
    monkeypatch.setenv("HEPHAESTUS_PROMPT_DIR", str(tmp_path))

    assert get_plan_prompt(12) == "environment 12\n"


def test_harness_can_override_a_shared_prompt_fragment(tmp_path: Path) -> None:
    """A shared fragment override applies inside an otherwise default prompt."""
    fragment = tmp_path / "shared" / "terse_output_directive.j2"
    fragment.parent.mkdir()
    fragment.write_text("HARNESS DIRECTIVE", encoding="utf-8")

    rendered = get_plan_prompt(5, catalog=PromptCatalog(override_root=tmp_path))

    assert "HARNESS DIRECTIVE" in rendered
    assert "Output discipline (token budget)" not in rendered
