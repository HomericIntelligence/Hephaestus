"""Tests for the pure/parse/context PR-review cores (pr_review_core.py).

Split out of ``test_pr_reviewer_posting.py`` in the #1823 omit-reduction wave:
these exercise the extracted, unit-covered cores
(:func:`gather_impl_review_context`, :func:`run_pr_review_analysis`,
:func:`review_pr_inline`) directly, patching the ``pr_review_core`` seams the
cores actually bind. The standalone ``PRReviewer`` class-method tests stay in
``test_pr_reviewer_posting.py``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.claude_invoke import parse_review_verdict
from hephaestus.automation.pr_review_core import (
    AGGRESSIVE_DIFF_BUDGET_CHARS,
    DEFAULT_DIFF_BUDGET_CHARS,
    budget_diff_for_prompt,
    gather_impl_review_context,
    review_pr_inline,
    run_pr_review_analysis,
)
from hephaestus.github.client import PromptTooLongError


def _make_diff_file(path: str, lines: int) -> str:
    # Padded to ~43 chars/line to match this repo's measured diff density
    # (git diff HEAD~15..HEAD -> 29,431 lines / 1,263,927 chars), so synthetic
    # fixtures sized in "diff lines" translate to realistic char counts.
    body = "\n".join(f"+line {i} {'x' * 30}" for i in range(lines))
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{body}\n"


# ---------------------------------------------------------------------------
# Extracted in-loop cores (Stage 2, #28) shared with the implementer session
# ---------------------------------------------------------------------------


class TestBudgetDiffForPrompt:
    """budget_diff_for_prompt trims whole files, largest-first, to fit a budget (#1847)."""

    def test_returns_unchanged_when_under_budget(self) -> None:
        diff = _make_diff_file("small.py", 5)
        assert budget_diff_for_prompt(diff, max_chars=DEFAULT_DIFF_BUDGET_CHARS) == diff

    def test_drops_largest_file_first_keeps_small_file(self) -> None:
        small = _make_diff_file("small.py", 5)
        large = _make_diff_file("large.py", 2000)
        diff = small + large
        # Budget only large enough for the small file plus fixed overhead slack.
        result = budget_diff_for_prompt(diff, max_chars=len(small) + 12_000 + 200)
        assert "small.py" in result
        assert "+line 0 " in result  # small file body kept verbatim
        assert "large.py (2003 diff lines, omitted)" in result
        assert "largest file(s) omitted" in result
        # The large file's actual body content must not appear in the kept diff.
        assert result.count("diff --git") == 1

    def test_preserves_preamble_before_first_file_header(self) -> None:
        preamble = "[... diff truncated at 2000000 chars ...]\n"
        large = _make_diff_file("large.py", 5000)
        diff = preamble + large
        result = budget_diff_for_prompt(diff, max_chars=12_500)
        assert result.startswith(preamble)
        assert "large.py" in result  # shows up in the skipped index, not the body

    def test_skipped_file_label_is_clean_b_side_path(self) -> None:
        large = _make_diff_file("pkg/mod.py", 3000)
        result = budget_diff_for_prompt(large, max_chars=12_100)
        assert "- pkg/mod.py (3003 diff lines, omitted)" in result
        assert "a/pkg/mod.py b/pkg/mod.py" not in result.split("[...")[-1]

    def test_no_file_headers_falls_back_to_flat_truncation(self) -> None:
        diff = "x" * 100_000
        result = budget_diff_for_prompt(diff, max_chars=12_500)
        assert result.startswith("x" * 100)
        assert "diff truncated at" in result

    def test_composed_body_chars_shrinks_effective_budget(self) -> None:
        diff = _make_diff_file("small.py", 5)
        # A large composed body eats the entire nominal budget, forcing
        # truncation even though the diff alone would have fit.
        result = budget_diff_for_prompt(
            diff, max_chars=DEFAULT_DIFF_BUDGET_CHARS, composed_body_chars=DEFAULT_DIFF_BUDGET_CHARS
        )
        assert "diff truncated at 0 chars" in result or "largest file(s) omitted" in result

    def test_pr_1846_sized_diff_fits_default_budget(self) -> None:
        """A diff sized like the issue's motivating PR (#1846) fits unchanged.

        ~4,800 diff lines at this repo's measured ~43 chars/line density is
        ~206,000 chars — well inside DEFAULT_DIFF_BUDGET_CHARS (350,000) even
        after the fixed overhead and a modest composed body are subtracted.
        """
        diff = _make_diff_file("big_feature.py", 4800)
        assert 190_000 <= len(diff) <= 220_000
        result = budget_diff_for_prompt(
            diff, max_chars=DEFAULT_DIFF_BUDGET_CHARS, composed_body_chars=10_000
        )
        assert result == diff  # unchanged: fits without truncation


class TestGatherImplReviewContext:
    """gather_impl_review_context folds TASK + PLAN + PLAN_REVIEW + diff together."""

    def test_composes_full_context(self) -> None:
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="Add widget",
            issue_body="The widget body.",
            plan_text="# Implementation Plan\nStep 1",
            plan_review_text="## 🔍 Plan Review\nVerdict: GO",
            diff_text="diff --git a/x b/x",
        )
        assert ctx["pr_diff"] == "diff --git a/x b/x"
        # TASK title + body and both PLAN sections are surfaced to the reviewer.
        assert "Add widget" in ctx["issue_body"]
        assert "The widget body." in ctx["issue_body"]
        assert "## PLAN" in ctx["issue_body"]
        assert "Step 1" in ctx["issue_body"]
        assert "## PLAN_REVIEW" in ctx["issue_body"]
        assert "Verdict: GO" in ctx["issue_body"]

    def test_missing_plan_sections_get_placeholders(self) -> None:
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan_text="",
            plan_review_text="",
            diff_text="",
        )
        assert "no plan comment found" in ctx["issue_body"]
        assert "no plan-review comment found" in ctx["issue_body"]

    def test_preserves_advise_findings_for_prompt(self) -> None:
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan_text="",
            plan_review_text="",
            diff_text="",
            advise_findings="prior team finding",
        )
        assert ctx["advise_findings"] == "prior team finding"

    def test_large_diff_is_budgeted_by_default(self) -> None:
        """A diff larger than the default budget is truncated, not embedded whole (#1847)."""
        large_diff = _make_diff_file("huge.py", 20_000)
        assert len(large_diff) > DEFAULT_DIFF_BUDGET_CHARS
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan_text="",
            plan_review_text="",
            diff_text=large_diff,
        )
        assert len(ctx["pr_diff"]) < len(large_diff)
        assert "largest file(s) omitted" in ctx["pr_diff"]

    def test_large_plan_shrinks_diff_allowance(self) -> None:
        """A large composed PLAN/PLAN_REVIEW body eats into the diff budget."""
        diff = _make_diff_file("small.py", 5)
        huge_plan = "x" * (DEFAULT_DIFF_BUDGET_CHARS)
        ctx = gather_impl_review_context(
            pr_number=42,
            issue_number=1,
            issue_title="t",
            issue_body="b",
            plan_text=huge_plan,
            plan_review_text="",
            diff_text=diff,
        )
        # The composed body alone consumes the whole nominal budget, so even
        # this small diff gets truncated instead of embedded whole.
        assert ctx["pr_diff"] != diff


class TestRunPrReviewAnalysis:
    """run_pr_review_analysis is the shared analysis core (standalone + in-loop)."""

    def test_dry_run_returns_placeholder(self, tmp_path: Path) -> None:
        out = run_pr_review_analysis(
            pr_number=1,
            issue_number=1,
            worktree_path=tmp_path,
            context={},
            agent="claude",
            state_dir=tmp_path,
            dry_run=True,
        )
        assert out["comments"] == []
        assert "DRY RUN" in out["summary"]
        assert out["review_text"] == out["summary"]

    def test_passes_review_agent_token_to_claude(self, tmp_path: Path) -> None:
        """The review_agent token is forwarded verbatim to invoke_claude_with_session."""
        captured: dict[str, str] = {}

        def _fake_invoke(*, agent: str, **_: object) -> tuple[str, str]:
            captured["agent"] = agent
            return (
                '{"result": "```json\\n{\\"comments\\": [], \\"summary\\": \\"ok\\"}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                review_agent="pr-reviewer-r1",
                state_dir=tmp_path,
                dry_run=False,
            )
        assert captured["agent"] == "pr-reviewer-r1"

    def test_error_envelope_propagates_not_parsed_as_verdict(self, tmp_path: Path) -> None:
        """An is_error:true envelope must raise, not be parsed into a bogus verdict.

        Guards the #1528 defence: the Claude CLI can exit 0 with an error
        envelope (e.g. a 429 quota cap); run_pr_review_analysis calls
        raise_for_error_envelope so the review-phase handler waits for reset
        instead of recording a silently-fabricated GO/NOGO. Assert the raised
        error propagates out of run_pr_review_analysis rather than being
        swallowed and turned into review text.
        """

        def _fake_invoke(**_: object) -> tuple[str, str]:
            return ('{"is_error": true, "result": "usage cap reached"}', "")

        def _raise(_stdout: str) -> None:
            raise RuntimeError("usage cap (#1528)")

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
            patch(
                "hephaestus.automation.pr_review_core.raise_for_error_envelope",
                side_effect=_raise,
            ),
        ):
            with pytest.raises(RuntimeError, match=r"usage cap"):
                run_pr_review_analysis(
                    pr_number=1,
                    issue_number=1,
                    worktree_path=tmp_path,
                    context={"pr_diff": "d"},
                    agent="claude",
                    review_agent="pr-reviewer-r1",
                    state_dir=tmp_path,
                    dry_run=False,
                )

    def test_passes_advise_findings_to_prompt_builder(self, tmp_path: Path) -> None:
        captured: dict[str, object] = {}

        def _fake_prompt(**kwargs: object) -> str:
            captured.update(kwargs)
            return "prompt"

        with (
            patch(
                "hephaestus.automation.pr_review_core.get_pr_review_analysis_prompt",
                side_effect=_fake_prompt,
            ),
            patch("hephaestus.automation.pr_review_core.run_agent_text") as mock_agent,
        ):
            mock_agent.return_value = MagicMock(
                stdout='Verdict: GO\n```json\n{"comments": [], "summary": "ok"}\n```'
            )
            run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"advise_findings": "prior team finding"},
                agent="codex",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert captured["advise_findings"] == "prior team finding"

    def test_claude_path_preserves_review_text_for_verdict(self, tmp_path: Path) -> None:
        """Claude JSON summary may omit Verdict, but full prose must be returned."""
        response_text = (
            "Detailed review.\n\nGrade: A\nVerdict: GO\n\n"
            "```json\n" + json.dumps({"comments": [], "summary": "No inline findings."}) + "\n```"
        )

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                return_value=(json.dumps({"result": response_text}), ""),
            ),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "diff"},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert out["summary"] == "No inline findings."
        assert "Verdict: GO" in out["review_text"]
        assert parse_review_verdict(out["review_text"]).verdict == "GO"

    def test_codex_path_preserves_stdout_for_verdict(self, tmp_path: Path) -> None:
        """Codex stdout prose must survive JSON parsing for verdict extraction."""
        stdout = (
            "Review complete.\n\nGrade: D\nVerdict: NOGO\n\n"
            "```json\n" + json.dumps({"comments": [], "summary": "Needs fixes."}) + "\n```"
        )

        with patch(
            "hephaestus.automation.pr_review_core.run_agent_text",
            return_value=MagicMock(stdout=stdout),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "diff"},
                agent="codex",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert out["summary"] == "Needs fixes."
        assert "Verdict: NOGO" in out["review_text"]
        assert parse_review_verdict(out["review_text"]).verdict == "NOGO"

    def test_uses_canonical_review_utils_parser_patch_target(self, tmp_path: Path) -> None:
        """PR-review parsing goes through the canonical patch target."""
        stdout = 'review prose\n```json\n{"comments": [], "summary": "real"}\n```'

        with (
            patch(
                "hephaestus.automation.pr_review_core.run_agent_text",
                return_value=MagicMock(stdout=stdout),
            ),
            patch(
                "hephaestus.automation._review_utils.parse_json_block",
                return_value={"comments": [], "summary": "patched"},
            ) as parse_json,
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "diff"},
                agent="codex",
                state_dir=tmp_path,
                dry_run=False,
            )

        parse_json.assert_called_once_with(stdout)
        assert out["summary"] == "patched"
        assert out["review_text"] == stdout

    def test_prompt_passed_via_stdin_not_argv(self, tmp_path: Path) -> None:
        """The reviewer prompt is piped via stdin, never embedded in argv.

        Regression for `[Errno 7] Argument list too long: 'claude'`: the
        PR-review prompt embeds the full diff and overflows ARG_MAX when passed
        as a positional argument, so the wrapper must be called with
        ``input_via_stdin=True``.
        """
        captured: dict[str, object] = {}

        def _fake_invoke(**kwargs: object) -> tuple[str, str]:
            captured.update(kwargs)
            return (
                '{"result": "```json\\n{\\"comments\\": [], \\"summary\\": \\"ok\\"}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "x" * 200_000},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )
        assert captured["input_via_stdin"] is True

    def test_retries_with_aggressive_budget_and_succeeds(self, tmp_path: Path) -> None:
        """PromptTooLongError triggers exactly one retry with a smaller diff, which succeeds."""
        calls: list[dict[str, object]] = []

        def _fake_invoke(*, prompt: str, **kwargs: object) -> tuple[str, str]:
            calls.append({"prompt": prompt, **kwargs})
            if len(calls) == 1:
                return ('{"is_error": true, "result": "Prompt is too long"}', "")
            return (
                '{"result": "```json\\n{\\"comments\\": [], \\"summary\\": \\"ok\\"}\\n```"}',
                "",
            )

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": _make_diff_file("big.py", 20_000), "issue_body": "x"},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )

        assert len(calls) == 2
        first_prompt = str(calls[0]["prompt"])
        second_prompt = str(calls[1]["prompt"])
        assert len(second_prompt) < len(first_prompt)
        # The retry prompt's diff portion is bounded by the aggressive budget.
        assert len(second_prompt) < AGGRESSIVE_DIFF_BUDGET_CHARS + 20_000
        assert out["summary"] == "ok"
        assert out["comments"] == []

    def test_aggressive_retry_also_fails_raises_reason_prompt_too_long(
        self, tmp_path: Path
    ) -> None:
        """If the aggressive retry ALSO reports too-long, raise once with a distinct reason."""

        def _always_too_long(**_: object) -> tuple[str, str]:
            return ('{"is_error": true, "result": "Prompt is too long"}', "")

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_always_too_long,
            ),
        ):
            with pytest.raises(RuntimeError, match="reason=prompt_too_long"):
                run_pr_review_analysis(
                    pr_number=1,
                    issue_number=1,
                    worktree_path=tmp_path,
                    context={"pr_diff": _make_diff_file("big.py", 20_000), "issue_body": "x"},
                    agent="claude",
                    state_dir=tmp_path,
                    dry_run=False,
                )

    def test_prompt_too_long_not_confused_with_usage_cap(self) -> None:
        """PromptTooLongError is a distinct type from ClaudeUsageCapError."""
        assert issubclass(PromptTooLongError, RuntimeError)


class TestReviewPrInline:
    """review_pr_inline runs a FRESH per-iteration reviewer and posts inline threads."""

    def test_posts_threads_and_returns_verdict(self, tmp_path: Path) -> None:
        analysis = {
            "comments": [{"path": "a.py", "line": 1, "body": "fix"}],
            "summary": "Findings for GitHub.",
            "review_text": "Full reviewer prose.\n\nGrade: C\nVerdict: NOGO\n",
        }
        with (
            patch(
                "hephaestus.automation.pr_review_core.run_pr_review_analysis",
                return_value=analysis,
            ) as mock_analysis,
            patch(
                "hephaestus.automation.pr_review_core.gh_pr_review_post",
                return_value=["thread-1"],
            ) as mock_post,
        ):
            summary, thread_ids = review_pr_inline(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                iteration=2,
                state_dir=tmp_path,
                dry_run=False,
            )

        assert thread_ids == ["thread-1"]
        assert "NOGO" in summary
        # FRESH per-iteration reviewer session: reviewer_agent(AGENT_PR_REVIEWER, 2).
        assert mock_analysis.call_args.kwargs["review_agent"] == "pr-reviewer-r2"
        mock_post.assert_called_once()
        assert mock_post.call_args.kwargs["pr_number"] == 42
        assert mock_post.call_args.kwargs["summary"] == "Findings for GitHub."

    def test_dry_run_skips_posting(self, tmp_path: Path) -> None:
        with patch("hephaestus.automation.pr_review_core.gh_pr_review_post") as mock_post:
            _summary, thread_ids = review_pr_inline(
                pr_number=42,
                issue_number=1,
                worktree_path=tmp_path,
                context={},
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                dry_run=True,
            )
        assert thread_ids == []
        mock_post.assert_not_called()


class TestVerdictFromProseNotSummary:
    """The Grade/Verdict pair lives in review prose, not the JSON summary.

    Regression for the AMBIGUOUS misread: review_pr_inline must return the
    verdict-bearing prose so parse_review_verdict sees `Grade`/`Verdict`, even
    though the JSON `summary` field (posted to GitHub) carries no verdict line.
    """

    def test_run_analysis_surfaces_review_text_with_verdict(self, tmp_path: Path) -> None:
        """run_pr_review_analysis returns the prose body (carrying Verdict:) as review_text."""
        prose = (
            "## Review\nFindings here.\n\n"
            "Grade: F\nVerdict: NOGO — two real defects.\n\n"
            '```json\n{"comments": [], "summary": "two defects (no verdict here)"}\n```'
        )
        # Claude wraps the prose in a JSON result envelope.
        envelope = json.dumps({"result": prose})

        def _fake_invoke(**_: object) -> tuple[str, str]:
            return (envelope, "")

        with (
            patch("hephaestus.automation.pr_review_core.get_repo_root", return_value=tmp_path),
            patch("hephaestus.automation.pr_review_core.get_repo_slug", return_value="Repo"),
            patch(
                "hephaestus.automation.pr_review_core.invoke_claude_with_session",
                side_effect=_fake_invoke,
            ),
        ):
            out = run_pr_review_analysis(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={"pr_diff": "d"},
                agent="claude",
                state_dir=tmp_path,
                dry_run=False,
            )
        # summary is the JSON field (no verdict); review_text is the prose (has verdict).
        assert out["summary"] == "two defects (no verdict here)"
        assert "Grade: F\nVerdict: NOGO" in out["review_text"]

    def test_review_pr_inline_returns_verdict_text_not_summary(self, tmp_path: Path) -> None:
        """review_pr_inline returns the verdict-bearing prose, so the loop parses NOGO."""
        from hephaestus.automation.claude_invoke import parse_review_verdict

        analysis = {
            "comments": [
                {"path": "a.py", "line": 1, "side": "RIGHT", "severity": "major", "body": "x"}
            ],
            "summary": "a defect (no verdict token here)",
            "review_text": "## Review\nProse.\n\nGrade: F\nVerdict: NOGO — a real defect.\n",
        }
        with (
            patch(
                "hephaestus.automation.pr_review_core.run_pr_review_analysis", return_value=analysis
            ),
            patch(
                "hephaestus.automation.pr_review_core.gh_pr_review_post", return_value=["thread-1"]
            ),
        ):
            review_text, thread_ids = review_pr_inline(
                pr_number=1,
                issue_number=1,
                worktree_path=tmp_path,
                context={},
                agent="claude",
                iteration=0,
                state_dir=tmp_path,
                dry_run=False,
            )
        # The returned text must carry the verdict so the loop reads NOGO, not AMBIGUOUS.
        assert parse_review_verdict(review_text).verdict == "NOGO"
        assert thread_ids == ["thread-1"]
