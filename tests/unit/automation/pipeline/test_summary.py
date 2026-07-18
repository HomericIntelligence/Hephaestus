"""Pipeline summary tests: rows, aggregates, preserved footer, JSON envelope (#1817).

Pins the exact :func:`format_preserved_worktrees` line sequence. The legacy
implementer summary printer that used to delegate to this helper was deleted
when ``hephaestus-implement-issues`` became a thin pipeline wrapper (#1821); the
pipeline summary is now the sole consumer.
"""

from __future__ import annotations

import json
import logging

import pytest

from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.summary import (
    RunStats,
    format_preserved_worktrees,
    print_summary,
)
from hephaestus.automation.pipeline.work_item import ItemKind, ItemResult, WorkItem


def _stats(**overrides: object) -> RunStats:
    defaults: dict[str, object] = {
        "exit_code": 0,
        "loops_run": 1,
        "agent_job_count": 3,
        "agent_job_time_s": 12.5,
        "wall_s": 40.0,
    }
    defaults.update(overrides)
    return RunStats(**defaults)  # type: ignore[arg-type]


class TestRunStats:
    """Derived run-stat fields."""

    def test_interrupted_is_derived_from_exit_code(self) -> None:
        assert _stats(exit_code=130).interrupted is True
        assert _stats(exit_code=1).interrupted is False


def _item(
    issue: int,
    stage: StageName,
    *,
    passed: bool | None = None,
    reason: str = "",
    pr: int | None = None,
) -> WorkItem:
    item = WorkItem(repo="repo-a", kind=ItemKind.ISSUE, issue=issue, pr=pr, stage=stage)
    item.payload["entry_stage"] = "planning"
    if passed is not None:
        item.result = ItemResult(passed=passed, reason=reason, final_stage=stage)
    return item


class TestFormatPreservedWorktrees:
    """Exact legacy line sequence, shared by both printers."""

    def test_empty_list_yields_no_lines(self) -> None:
        assert format_preserved_worktrees([], "script.py") == []

    def test_line_sequence_matches_legacy(self) -> None:
        preserved = [("repo-a", 101, "/wt/issue-101"), ("repo-b", 202, "/wt/issue-202")]

        lines = format_preserved_worktrees(preserved, "impl.py")

        assert lines == [
            "\nPreserved worktrees (contain uncommitted changes):",
            "  #101: /wt/issue-101",
            "  #202: /wt/issue-202",
            "\nRerun these issues after inspecting/cleaning the worktrees:",
            "  impl.py --issues 101,202",
            "To discard them instead:",
            "  git worktree remove --force /wt/issue-101",
            "  git worktree remove --force /wt/issue-202",
        ]

    def test_rerun_hint_actually_parses_with_the_loop_cli(self) -> None:
        """The emitted rerun command must be valid input to the loop parser (#2281).

        A byte-identical pin previously froze a command the CLI rejects
        (space-joined ``--issues`` + a nonexistent ``--resume``). Assert the
        real argparser accepts the emitted flags and recovers the numbers.
        """
        from hephaestus.automation import loop_runner

        preserved = [("repo-a", 101, "/wt/issue-101"), ("repo-b", 202, "/wt/issue-202")]
        rerun_line = next(
            line for line in format_preserved_worktrees(preserved, "loop") if "--issues" in line
        )
        # Everything after the script token is the argv the operator would run.
        argv = rerun_line.split()[1:]
        parsed = loop_runner._parse_args(argv)
        assert parsed.issues == [101, 202]


class TestPrintSummaryRows:
    """Per-item rows and aggregates."""

    def test_all_disposition_rows(self, caplog: pytest.LogCaptureFixture) -> None:
        """PASS / FAIL:reason / SKIP / BLOCKED / RESUMABLE rows all render."""
        items = [
            _item(1, StageName.FINISHED, passed=True, reason="merged", pr=11),
            _item(2, StageName.FINISHED, passed=False, reason="tests failed"),
            _item(3, StageName.FINISHED, passed=False, reason="skip: state:skip"),
            _item(4, StageName.FINISHED, passed=False, reason="blocked: human threads"),
            _item(5, StageName.PR_REVIEW, passed=False, reason="resumable at pr_review"),
        ]

        with caplog.at_level(logging.INFO):
            print_summary(items, _stats(), [], json_out=False)

        text = caplog.text
        assert "PASS" in text
        assert "FAIL:tests failed" in text
        assert "SKIP" in text
        assert "BLOCKED" in text
        assert "RESUMABLE at pr_review" in text
        assert "#1" in text and "!11" in text

    def test_aggregates_and_stats(self, caplog: pytest.LogCaptureFixture) -> None:
        """Disposition counts, per-stage throughput, agent time, loops, wall."""
        items = [
            _item(1, StageName.FINISHED, passed=True, reason="ok"),
            _item(2, StageName.FINISHED, passed=True, reason="ok"),
            _item(3, StageName.FINISHED, passed=False, reason="boom"),
        ]

        with caplog.at_level(logging.INFO):
            print_summary(
                items, _stats(loops_run=2, agent_job_count=5, wall_s=99.5), [], json_out=False
            )

        text = caplog.text
        assert "'pass': 2" in text
        assert "'fail': 1" in text
        assert "agent jobs: 5" in text
        assert "loops: 2" in text
        assert "wall: 99.5s" in text

    def test_summary_uses_latest_logical_item_for_aggregates(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A later PASS for the same issue/PR supersedes an earlier failed attempt."""
        failed = _item(2009, StageName.FINISHED, passed=False, reason="git_error", pr=2010)
        passed = _item(2009, StageName.FINISHED, passed=True, reason="merged", pr=2010)

        with caplog.at_level(logging.INFO):
            print_summary([failed, passed], _stats(), [], json_out=False)

        text = caplog.text
        assert "items: 1" in text
        assert "'pass': 1" in text
        assert "'fail'" not in text
        assert "FAIL:git_error" not in text

    def test_summary_supersedes_direct_pr_items_with_issue_context(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A direct PR seed with hydrated issue context uses one logical key."""
        failed = WorkItem(
            repo="repo-a",
            kind=ItemKind.PR,
            issue=2009,
            pr=2010,
            stage=StageName.FINISHED,
            result=ItemResult(passed=False, reason="git_error", final_stage=StageName.FINISHED),
        )
        passed = WorkItem(
            repo="repo-a",
            kind=ItemKind.PR,
            issue=2009,
            pr=2010,
            stage=StageName.FINISHED,
            result=ItemResult(passed=True, reason="merged", final_stage=StageName.FINISHED),
        )

        with caplog.at_level(logging.INFO):
            print_summary([failed, passed], _stats(), [], json_out=False)

        text = caplog.text
        assert "items: 1" in text
        assert "'pass': 1" in text
        assert "FAIL:git_error" not in text

    def test_nonzero_attempts_render(self, caplog: pytest.LogCaptureFixture) -> None:
        """Only non-zero attempt counters appear in the row."""
        item = _item(6, StageName.FINISHED, passed=True, reason="ok")
        item.attempts["plan"] = 2

        with caplog.at_level(logging.INFO):
            print_summary([item], _stats(), [], json_out=False)

        assert "plan=2" in caplog.text
        assert "ci_fix=" not in caplog.text

    def test_item_without_result_renders_pending(self, caplog: pytest.LogCaptureFixture) -> None:
        """A never-finished item (no result) renders PENDING, never crashes."""
        with caplog.at_level(logging.INFO):
            print_summary([_item(9, StageName.PLANNING)], _stats(), [], json_out=False)

        assert "PENDING" in caplog.text

    def test_preserved_footer_present(self, caplog: pytest.LogCaptureFixture) -> None:
        """The preserved-worktree footer prints via the shared helper."""
        with caplog.at_level(logging.INFO):
            print_summary([], _stats(exit_code=1), [("repo-a", 9, "/wt/9")], json_out=False)

        assert "Preserved worktrees (contain uncommitted changes):" in caplog.text
        assert "git worktree remove --force /wt/9" in caplog.text


class TestJsonEnvelope:
    """emit_json_status extension fields."""

    def test_json_envelope_fields(self, capsys: pytest.CaptureFixture[str]) -> None:
        """The envelope carries dispositions, loops, resumable, preserved."""
        items = [
            _item(1, StageName.FINISHED, passed=True, reason="ok"),
            _item(2, StageName.PR_REVIEW, passed=False, reason="resumable at pr_review"),
        ]

        print_summary(
            items,
            _stats(exit_code=130, loops_run=3),
            [("repo-a", 2, "/wt/2")],
            json_out=True,
        )

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope["exit_code"] == 130
        assert envelope["status"] == "error"
        assert envelope["message"] == "pipeline interrupted"
        assert envelope["dispositions"] == {"pass": 1, "resumable": 1}
        assert envelope["loops_run"] == 3
        assert envelope["resumable"] == ["repo-a#2@pr_review"]
        assert envelope["preserved_worktrees"] == [[2, "/wt/2"]]

    @pytest.mark.parametrize(
        ("exit_code", "expected_message"),
        [
            (0, "pipeline complete"),
            (1, "pipeline failed"),
        ],
    )
    def test_json_envelope_message_tracks_exit_code(
        self,
        capsys: pytest.CaptureFixture[str],
        exit_code: int,
        expected_message: str,
    ) -> None:
        """The envelope message derives from exit_code."""
        print_summary(
            [],
            _stats(exit_code=exit_code),
            [],
            json_out=True,
        )

        envelope = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert envelope["exit_code"] == exit_code
        assert envelope["message"] == expected_message

    def test_no_envelope_without_json_out(self, capsys: pytest.CaptureFixture[str]) -> None:
        """json_out=False never writes the JSON envelope to stdout."""
        print_summary([], _stats(), [], json_out=False)

        assert capsys.readouterr().out == ""
