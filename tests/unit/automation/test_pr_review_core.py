"""Tests for the pure/parse/context PR-review cores (pr_review_core.py).

Extracted in the #1823 omit-reduction wave, these exercise the unit-covered cores
(:func:`gather_impl_review_context`, :func:`run_pr_review_analysis`) directly,
patching the ``pr_review_core`` seams the cores actually bind.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.automation.pr_review_core import (
    AGGRESSIVE_DIFF_BUDGET_CHARS,
    DEFAULT_DIFF_BUDGET_CHARS,
    MAX_PR_REVIEW_RENDERED_CHARS,
    _compact_host_verifications_json,
    budget_diff_for_prompt,
    build_bounded_pr_review_analysis_prompt,
    gather_impl_review_context,
    run_pr_review_analysis,
)
from hephaestus.automation.review_audit import ReviewAudit
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

    def test_claude_review_scope_can_run_athena_pr_review_skill(self, tmp_path: Path) -> None:
        """The legacy review path keeps the skill's read-only helper surface."""
        captured: dict[str, str] = {}

        def _fake_invoke(**kwargs: object) -> tuple[str, str]:
            captured["allowed_tools"] = str(kwargs["allowed_tools"])
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
                state_dir=tmp_path,
                dry_run=False,
            )

        assert captured["allowed_tools"] == "Read,Glob,Grep,Bash,Skill,Agent,WebFetch"

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
                stdout=(
                    '```json\n{"grade": "A", "verdict": "GO", "comments": [], "summary": "ok"}\n```'
                )
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

    def test_claude_path_uses_structural_audit(self, tmp_path: Path) -> None:
        """Claude JSON output produces a structural audit; prose is supplemental."""
        response_text = (
            "Detailed review.\n\nGrade: A\nVerdict: GO\n\n"
            "```json\n"
            + json.dumps(
                {
                    "grade": "A",
                    "verdict": "GO",
                    "comments": [],
                    "summary": "No inline findings.",
                }
            )
            + "\n```"
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
        assert out["audit"].valid is True
        assert "Verdict:" not in out["review_text"]
        assert out["audit"].grade == "A"

    def test_codex_path_uses_structural_audit(self, tmp_path: Path) -> None:
        """Codex stdout uses the same structural audit contract."""
        stdout = (
            "Review complete.\n\nGrade: D\nVerdict: NOGO\n\n"
            "```json\n"
            + json.dumps(
                {
                    "grade": "D",
                    "verdict": "NOGO",
                    "comments": [],
                    "summary": "Needs fixes.",
                }
            )
            + "\n```"
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
        assert out["audit"].valid is True
        assert "Verdict:" not in out["review_text"]
        assert out["audit"].grade == "D"

    def test_uses_canonical_review_utils_parser_patch_target(self, tmp_path: Path) -> None:
        """PR-review parsing goes through the canonical patch target."""
        stdout = 'review prose\n```json\n{"grade":"B","comments": [], "summary": "real"}\n```'

        with (
            patch(
                "hephaestus.automation.pr_review_core.run_agent_text",
                return_value=MagicMock(stdout=stdout),
            ),
            patch(
                "hephaestus.automation.pr_review_core.parse_review_audit",
                return_value=ReviewAudit("B", "patched", (), "review prose", True),
            ) as parse_audit,
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

        parse_audit.assert_called_once_with(stdout)
        assert out["summary"] == "patched"
        assert out["review_text"] == "review prose"

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
            review_json = json.dumps(
                {"grade": "A", "verdict": "GO", "comments": [], "summary": "ok"}
            )
            return (
                json.dumps({"result": f"```json\n{review_json}\n```"}),
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
            review_json = json.dumps(
                {"grade": "A", "verdict": "GO", "comments": [], "summary": "ok"}
            )
            return (
                json.dumps({"result": f"```json\n{review_json}\n```"}),
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


def test_bounded_review_prompt_preserves_receipts_without_exceeding_agent_limit() -> None:
    """Large diffs and many host receipts cannot overflow a direct reviewer request."""
    host_receipts = json.dumps(
        [
            {
                "argv": ["uv", "run", "pytest", f"tests/unit/test_{index}.py"],
                "head_sha": "a" * 40,
                "immutable_source": True,
                "ok": True,
                "status": "passed",
                "stdout_tail": "s" * 4_000,
                "stderr_tail": "e" * 4_000,
            }
            for index in range(35)
        ]
    )

    prompt = build_bounded_pr_review_analysis_prompt(
        pr_number=2755,
        issue_number=2705,
        pr_diff=_make_diff_file("large.py", 20_000),
        issue_body="issue context",
        host_verifications_json=host_receipts,
    )

    assert len(prompt) <= MAX_PR_REVIEW_RENDERED_CHARS
    assert "[... host verification output truncated ...]" in prompt
    assert "tests/unit/test_0.py" in prompt
    assert "tests/unit/test_34.py" in prompt
    assert "[... PR diff truncated ...]" in prompt


def test_host_receipt_overflow_keeps_valid_identity_records() -> None:
    """The overflow summary keeps one identity record for each receipt."""
    receipts = [
        {
            "argv": [
                "uv",
                "run",
                "pytest",
                "-o",
                "addopts=",
                f"tests/unit/test_{index}_{'x' * 220}.py",
                "-q",
            ],
            "head_sha": "a" * 40,
            "immutable_source": True,
            "failure_kind": "none",
            "ok": True,
            "platform": "darwin",
            "status": "passed",
            "stdout_tail": "s" * 4_000,
            "stderr_tail": "e" * 4_000,
        }
        for index in range(240)
    ]

    compacted_json = _compact_host_verifications_json(json.dumps(receipts))
    compacted = json.loads(compacted_json)

    assert compacted["summary_policy"] == "host-receipt-digests-v1"
    assert compacted["receipt_count"] == len(receipts)
    identity_fields = [
        "argv",
        "head_sha",
        "immutable_source",
        "ok",
        "status",
        "platform",
        "failure_kind",
    ]
    assert compacted["identity_fields"] == identity_fields
    assert len(compacted["receipt_digests"]) == len(receipts)
    assert all(isinstance(digest, str) and digest for digest in compacted["receipt_digests"])


def test_bounded_review_prompt_caps_extreme_receipt_count() -> None:
    """The digest summary keeps an extreme receipt stream under the prompt cap."""
    receipts = [
        {
            "argv": ["uv", "run", "pytest", f"tests/unit/test_{index}_{'x' * 220}.py"],
            "head_sha": "a" * 40,
            "immutable_source": True,
            "failure_kind": "none",
            "ok": True,
            "platform": "darwin",
            "status": "passed",
            "stdout_tail": "s" * 4_000,
            "stderr_tail": "e" * 4_000,
        }
        for index in range(1_000)
    ]
    host_verifications_json = json.dumps(receipts)

    compacted = json.loads(_compact_host_verifications_json(host_verifications_json))
    prompt = build_bounded_pr_review_analysis_prompt(
        pr_number=2755,
        issue_number=2705,
        host_verifications_json=host_verifications_json,
    )

    assert compacted["summary_policy"] == "host-receipt-digests-v1"
    assert compacted["receipt_count"] == len(receipts)
    assert len(compacted["receipt_digests"]) == len(receipts)
    assert len(prompt) <= MAX_PR_REVIEW_RENDERED_CHARS


def test_host_receipt_aggregate_summary_is_bounded_and_valid() -> None:
    """The aggregate policy records count and identity for a very large stream."""
    receipts = [
        {
            "argv": ["pytest", f"tests/unit/test_{index}.py"],
            "head_sha": "a" * 40,
            "immutable_source": True,
            "ok": True,
            "status": "passed",
        }
        for index in range(2_000)
    ]

    compacted = json.loads(_compact_host_verifications_json(json.dumps(receipts)))

    assert compacted["summary_policy"] == "host-receipt-aggregate-v1"
    assert compacted["receipt_count"] == len(receipts)
    assert len(compacted["identity_sha256"]) == 64
    assert len(json.dumps(compacted, separators=(",", ":"))) <= 64_000


class TestStructuralAuditNotProse:
    """Structural audit fields drive review handling; prose is supplemental."""

    def test_run_analysis_strips_decision_tokens_from_prose(self, tmp_path: Path) -> None:
        """Decision-shaped prose cannot be retained beside the structural audit."""
        prose = (
            "## Review\nFindings here.\n\n"
            "Grade: F\nVerdict: NOGO — two real defects.\n\n"
            '```json\n{"grade": "F", "verdict": "BLOCKED", "comments": [], '
            '"summary": "two defects"}\n```'
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
        assert out["summary"] == "two defects"
        assert out["audit"].grade == "F"
        assert "Verdict: NOGO" not in out["review_text"]
