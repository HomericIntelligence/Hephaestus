"""Tests for hephaestus.automation.prompts.

Prompt builders are pure functions returning formatted strings — verify
each one substitutes its arguments and renders without ``KeyError`` on
common edge-case inputs (e.g. content containing curly braces).
"""

from __future__ import annotations

from hephaestus.automation import prompts


class TestImplementationPrompt:
    """Tests for implementation prompt."""

    def test_substitutes_issue_number(self) -> None:
        out = prompts.get_implementation_prompt(
            issue_number=42,
            issue_title="title",
            issue_body="body",
            branch_name="branch",
            worktree_path="/tmp/wt",
        )
        assert "42" in out
        assert "title" in out
        assert "body" in out
        assert "branch" in out
        assert "/tmp/wt" in out

    def test_optional_args_default(self) -> None:
        out = prompts.get_implementation_prompt(issue_number=1)
        assert "1" in out


class TestPlanPrompt:
    """Tests for plan prompt."""

    def test_substitutes_issue_number(self) -> None:
        out = prompts.get_plan_prompt(99)
        assert "99" in out


class TestAdvisePrompt:
    """Tests for advise prompt."""

    def test_substitutes_all_fields(self) -> None:
        out = prompts.get_advise_prompt(
            issue_number=7,
            issue_title="t",
            issue_body="b",
            marketplace_path="/mp.json",
        )
        assert "7" in out
        assert "/mp.json" in out


class TestFollowUpPrompt:
    """Tests for follow up prompt."""

    def test_substitutes_issue_number(self) -> None:
        out = prompts.get_follow_up_prompt(123)
        assert "123" in out

    def test_declares_scope_categories(self) -> None:
        out = prompts.get_follow_up_prompt(1)
        # The four categories the parser will accept must all be named
        # explicitly in the prompt.
        for category in ("core", "security", "safety", "critical_bug"):
            assert category in out

    def test_explicitly_rejects_feature_expansion(self) -> None:
        out = prompts.get_follow_up_prompt(1)
        # The prompt must explicitly tell Claude NOT to file follow-ups for
        # feature expansion / nice-to-haves / documentation polish.
        assert "OUT OF SCOPE" in out or "out of scope" in out.lower()
        assert "rejected" in out.lower()
        # Output schema is the new sectioned object (not the legacy flat array)
        assert "follow_ups" in out
        assert "category" in out


class TestPRDescription:
    """Tests for p r description."""

    def test_basic_description(self) -> None:
        out = prompts.get_pr_description(issue_number=5, summary="s", changes="c", testing="t")
        assert "Closes #5" in out
        assert "s" in out and "c" in out and "t" in out

    def test_curly_braces_in_content_do_not_crash(self) -> None:
        # Regression: get_pr_description uses f-string concatenation precisely
        # to avoid KeyError on ``{...}`` content like code blocks.
        out = prompts.get_pr_description(
            issue_number=1,
            summary="foo {bar} baz",
            changes="a {b} c",
            testing="x {y} z",
        )
        assert "{bar}" in out
        assert "{b}" in out
