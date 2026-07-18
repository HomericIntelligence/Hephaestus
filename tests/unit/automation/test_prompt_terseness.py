"""Verify the shared terse-output directive is composed into every agent prompt (#1082)."""

from __future__ import annotations

import pathlib

import pytest

from hephaestus.automation.prompts import (
    get_address_review_prompt,
    get_advise_prompt,
    get_codex_advise_prompt,
    get_comment_difficulty_prompt,
    get_follow_up_prompt,
    get_impl_loop_review_prompt,
    get_impl_resume_feedback_prompt,
    get_implementation_prompt,
    get_plan_loop_review_prompt,
    get_plan_prompt,
    get_plan_review_prompt,
    get_pr_review_analysis_prompt,
    get_review_validation_prompt,
)
from hephaestus.automation.prompts._shared import get_terse_output_directive

# Distinctive phrase from the directive; verified at plan time to be absent
# from every existing prompt file (grep -rnE "Output discipline|token budget"
# hephaestus/automation/prompts/ returned no hits, exit 1).
SENTINEL = "Output discipline (token budget)"


def test_terse_directive_leads_with_github_carveout() -> None:
    """Carve-out MUST be the first non-blank line so brevity never truncates pr-policy artifacts."""
    first_line = get_terse_output_directive().lstrip().splitlines()[0]
    assert "GitHub-posted" in first_line
    assert "retain full detail" in first_line


# Each lambda's kwargs match the exact signature read from the source at plan
# time. Required kwargs are provided with minimal sentinel values; optionals
# are omitted.
PROMPT_BUILDERS = [
    lambda: get_plan_prompt(issue_number=1),
    lambda: get_plan_review_prompt(issue_number=1, issue_title="t", issue_body="b", plan_text="p"),
    lambda: get_plan_loop_review_prompt(
        issue_number=1,
        issue_title="t",
        issue_body="b",
        plan_text="p",
        learnings="",
        iteration=0,
        prior_review=None,
    ),
    lambda: get_implementation_prompt(issue_number=1),
    lambda: get_impl_loop_review_prompt(
        issue_number=1,
        issue_title="t",
        issue_body="b",
        diff_text="",
        files_changed="",
        iteration=0,
        prior_review=None,
    ),
    lambda: get_impl_resume_feedback_prompt(
        issue_number=1, prev_iteration=0, verdict="NOGO", review_text=""
    ),
    lambda: get_pr_review_analysis_prompt(pr_number=1, issue_number=1),
    lambda: get_review_validation_prompt(pr_number=1, issue_number=1, prior_comments_json="[]"),
    lambda: get_address_review_prompt(
        pr_number=1, issue_number=1, worktree_path="/x", threads_json="[]"
    ),
    lambda: get_follow_up_prompt(issue_number=1),
    lambda: get_advise_prompt(
        issue_number=1,
        issue_title="t",
        issue_body="b",
        marketplace_path="m.json",
    ),
    lambda: get_codex_advise_prompt(
        issue_number=1,
        issue_title="t",
        issue_body="b",
        marketplace_path="m.json",
    ),
    lambda: get_comment_difficulty_prompt(issue_number=1, comments_json="[]"),
]


@pytest.mark.parametrize("build", PROMPT_BUILDERS)
def test_each_prompt_includes_terse_directive(build) -> None:
    """Verify terse directive is composed into each agent prompt."""
    text = build()
    assert SENTINEL in text, "shared terse directive missing from composed prompt"


def test_terse_directive_is_external_to_python_prompt_modules() -> None:
    """The directive lives in a shared template, not a Python literal."""
    pkg = pathlib.Path(__file__).parents[3] / "hephaestus" / "automation" / "prompts"
    for py in pkg.glob("*.py"):
        body = py.read_text()
        assert "_TERSE_OUTPUT_DIRECTIVE =" not in body, (
            f"{py.name} re-defines the directive instead of rendering the shared template"
        )
