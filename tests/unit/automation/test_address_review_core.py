"""Focused unit tests for address-review parsing and fix-session helpers."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.agents.runtime import AgentRunResult
from hephaestus.automation import address_review_core as core
from hephaestus.automation.session_naming import AGENT_IMPLEMENTER


def _thread() -> dict[str, Any]:
    """Return one representative unresolved review thread."""
    return {"id": "t1", "path": "a.py", "line": 7, "body": "rework locking"}


class TestParseAddressedBlock:
    """_parse_addressed_block is the trace-free shared JSON parser."""

    def test_extracts_last_block(self) -> None:
        payload = {"addressed": ["t1"], "replies": {"t1": "fixed"}}
        text = "```json\n{}\n```\nmore\n```json\n" + json.dumps(payload) + "\n```"

        assert core._parse_addressed_block(text)["addressed"] == ["t1"]

    def test_no_block_defaults(self) -> None:
        assert core._parse_addressed_block("no json") == {"addressed": [], "replies": {}}

    def test_invalid_json_defaults(self) -> None:
        assert core._parse_addressed_block("```json\n{bad}\n```") == {
            "addressed": [],
            "replies": {},
        }


class TestResolveAddressedThreads:
    """resolve_addressed_threads retains its hallucination guard."""

    def test_resolves_only_presented_threads(self) -> None:
        with patch.object(core, "gh_pr_resolve_thread") as mock_resolve:
            core.resolve_addressed_threads(
                ["t-real", "t-hallucinated"],
                {"t-real": "fixed"},
                {"t-real"},
                dry_run=False,
            )

        mock_resolve.assert_called_once_with("t-real", dry_run=False)

    def test_forwards_dry_run(self) -> None:
        with patch.object(core, "gh_pr_resolve_thread") as mock_resolve:
            core.resolve_addressed_threads(["t1"], {"t1": "r"}, {"t1"}, dry_run=True)

        mock_resolve.assert_called_once_with("t1", dry_run=True)


class TestBuildAddressFixPrompt:
    """Tests for prompt construction and classifier forwarding."""

    def test_serializes_threads_and_forwards_context(self, tmp_path: Path) -> None:
        prompt = "address review prompt"
        with (
            patch.object(core, "classify_comments", return_value={"t1": "hard"}) as classify,
            patch.object(core, "format_todo_line", return_value="todo line") as format_todo,
            patch.object(core, "get_address_review_prompt", return_value=prompt) as get_prompt,
        ):
            result = core._build_address_fix_prompt(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                state_dir=tmp_path / "state",
                advise_timeout=31,
                task_block="task",
                task_review_block="review",
                diff_text="diff",
                unaddressed_findings=[{"id": "prior"}],
            )

        assert result == prompt
        classify.assert_called_once_with(
            threads=[_thread()],
            agent="claude",
            issue_number=1,
            worktree_path=tmp_path,
            repo_root=tmp_path,
            state_dir=tmp_path / "state",
            advise_timeout=31,
        )
        format_todo.assert_called_once_with(_thread(), "hard")
        assert json.loads(get_prompt.call_args.kwargs["threads_json"]) == [
            {"thread_id": "t1", "path": "a.py", "line": 7, "body": "rework locking"}
        ]
        assert get_prompt.call_args.kwargs == {
            "pr_number": 42,
            "issue_number": 1,
            "worktree_path": str(tmp_path),
            "threads_json": get_prompt.call_args.kwargs["threads_json"],
            "todo_block": "todo line",
            "task_block": "task",
            "task_review_block": "review",
            "diff_text": "diff",
            "unaddressed_findings": [{"id": "prior"}],
        }

    def test_uses_medium_todo_when_classification_is_missing(self, tmp_path: Path) -> None:
        with (
            patch.object(core, "classify_comments", return_value={}),
            patch.object(core, "format_todo_line", return_value="todo line") as format_todo,
            patch.object(core, "get_address_review_prompt", return_value="prompt"),
        ):
            core._build_address_fix_prompt(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                state_dir=tmp_path / "state",
                advise_timeout=31,
                task_block="",
                task_review_block="",
                diff_text="",
                unaddressed_findings=None,
            )

        format_todo.assert_called_once_with(_thread(), "medium")


class TestAddressFixPromptFile:
    """Tests for the secure prompt-file lifecycle."""

    def test_removes_prompt_file_after_context(self, tmp_path: Path) -> None:
        prompt_file = tmp_path / ".claude-address-review-1.md"

        with core._address_fix_prompt_file(tmp_path, 1, "prompt") as result:
            assert result == prompt_file
            assert result.read_text() == "prompt"

        assert not prompt_file.exists()

    def test_ignores_missing_prompt_file_during_cleanup(self, tmp_path: Path) -> None:
        with core._address_fix_prompt_file(tmp_path, 1, "prompt") as prompt_file:
            prompt_file.unlink()

    def test_warns_when_prompt_file_cannot_be_removed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with patch.object(Path, "unlink", side_effect=OSError("denied")):
            with core._address_fix_prompt_file(tmp_path, 1, "prompt"):
                pass

        assert "Could not unlink prompt file" in caplog.text


class TestInvokeAddressFixSession:
    """Tests for the isolated provider-dispatch boundary."""

    @pytest.mark.parametrize(
        ("session_id", "expected_log"),
        [
            ("session-1", "SESSION_ID: session-1\n\ndirect response"),
            (None, "direct response"),
        ],
    )
    def test_direct_agent_normalizes_response_and_optional_session_log(
        self,
        tmp_path: Path,
        session_id: str | None,
        expected_log: str,
    ) -> None:
        result = AgentRunResult(stdout="direct response", stderr="", session_id=session_id)
        with (
            patch.object(core, "uses_direct_agent_runner", return_value=True),
            patch.object(core, "direct_agent_model", return_value="direct-model") as model,
            patch.object(core, "run_agent_session", return_value=result) as run,
        ):
            output = core._invoke_address_fix_session(
                issue_number=1,
                worktree_path=tmp_path,
                agent="codex",
                repo_root=tmp_path,
                prompt="prompt",
                timeout=45,
            )

        assert output == core._AddressFixSessionOutput(
            response_text="direct response",
            log_text=expected_log,
        )
        model.assert_called_once_with("codex", "HEPH_IMPLEMENTER_MODEL")
        run.assert_called_once_with(
            agent="codex",
            prompt="prompt",
            cwd=tmp_path,
            timeout=45,
            model="direct-model",
            sandbox="workspace-write",
        )

    @pytest.mark.parametrize(
        ("stdout", "expected_response"),
        [
            ('{"result": "parsed response"}', "parsed response"),
            ("not a JSON envelope", "not a JSON envelope"),
        ],
    )
    def test_claude_agent_preserves_raw_log_and_extracts_response(
        self, tmp_path: Path, stdout: str, expected_response: str
    ) -> None:
        with (
            patch.object(core, "uses_direct_agent_runner", return_value=False),
            patch.object(core, "get_repo_slug", return_value="owner/repo"),
            patch.object(core, "implementer_model", return_value="claude-model"),
            patch.object(core, "invoke_claude_with_session", return_value=(stdout, "")) as invoke,
        ):
            output = core._invoke_address_fix_session(
                issue_number=1,
                worktree_path=tmp_path,
                agent="claude",
                repo_root=tmp_path,
                prompt="prompt",
                timeout=45,
            )

        assert output == core._AddressFixSessionOutput(
            response_text=expected_response,
            log_text=stdout,
        )
        invoke.assert_called_once_with(
            repo="owner/repo",
            issue=1,
            agent=AGENT_IMPLEMENTER,
            prompt="prompt",
            model="claude-model",
            cwd=tmp_path,
            timeout=45,
            output_format="json",
            permission_mode="dontAsk",
            allowed_tools="Read,Write,Edit,Glob,Grep,Bash,Task,Skill",
            input_via_stdin=True,
        )


class TestRunAddressFixSession:
    """Tests for the thin coordinator and its fail-closed dry-run guard."""

    def test_dry_run_short_circuits_all_responsibilities(self, tmp_path: Path) -> None:
        parse_fn = MagicMock()
        log_file = tmp_path / "address.log"

        with (
            patch.object(core, "_build_address_fix_prompt") as build_prompt,
            patch.object(core, "_invoke_address_fix_session") as invoke,
            patch.object(core, "write_secure") as write_secure,
        ):
            result = core.run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=parse_fn,
                log_file=log_file,
                dry_run=True,
            )

        assert result == {"addressed": [], "replies": {}}
        build_prompt.assert_not_called()
        invoke.assert_not_called()
        parse_fn.assert_not_called()
        write_secure.assert_not_called()
        assert not log_file.exists()
        assert not (tmp_path / ".claude-address-review-1.md").exists()

    def test_persists_response_before_parsing_and_removes_prompt_file(self, tmp_path: Path) -> None:
        events: list[str] = []
        output = MagicMock(response_text="response", log_text="raw log")

        @contextmanager
        def prompt_file(*_args: object) -> Iterator[Path]:
            events.append("enter")
            yield tmp_path / "prompt.md"
            events.append("exit")

        def persist(*_args: object) -> None:
            events.append("log")

        def parse(*_args: object, **_kwargs: object) -> dict[str, Any]:
            events.append("parse")
            return {"addressed": [], "replies": {}}

        with (
            patch.object(core, "_build_address_fix_prompt", return_value="prompt"),
            patch.object(core, "_address_fix_prompt_file", side_effect=prompt_file),
            patch.object(core, "_invoke_address_fix_session", return_value=output),
            patch.object(
                core,
                "_persist_address_fix_log",
                side_effect=persist,
            ),
            patch.object(
                core,
                "_parse_address_fix_session_output",
                side_effect=parse,
            ),
        ):
            result = core.run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=MagicMock(),
                log_file=tmp_path / "address.log",
            )

        assert result == {"addressed": [], "replies": {}}
        assert events == ["enter", "log", "parse", "exit"]

    def test_removes_prompt_file_after_parser_failure(self, tmp_path: Path) -> None:
        log_file = tmp_path / "address.log"
        parse_error = ValueError("invalid response")

        with (
            patch.object(core, "_build_address_fix_prompt", return_value="prompt"),
            patch.object(
                core,
                "_invoke_address_fix_session",
                return_value=core._AddressFixSessionOutput("response", "raw log"),
            ),
            pytest.raises(ValueError, match="invalid response"),
        ):
            core.run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=MagicMock(side_effect=parse_error),
                log_file=log_file,
            )

        assert log_file.read_text() == "raw log"
        assert not (tmp_path / ".claude-address-review-1.md").exists()

    def test_removes_prompt_file_after_provider_failure(self, tmp_path: Path) -> None:
        log_file = tmp_path / "address.log"
        provider_error = subprocess.CalledProcessError(
            2,
            ["agent"],
            output="stdout",
            stderr="stderr",
        )

        with (
            patch.object(core, "_build_address_fix_prompt", return_value="prompt"),
            patch.object(core, "_invoke_address_fix_session", side_effect=provider_error),
            pytest.raises(RuntimeError, match="Fix session failed for PR Hephaestus#42: stderr"),
        ):
            core.run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=MagicMock(),
                log_file=log_file,
            )

        assert log_file.read_text() == "EXIT CODE: 2\n\nSTDOUT:\nstdout\n\nSTDERR:\nstderr"
        assert not (tmp_path / ".claude-address-review-1.md").exists()

    def test_removes_prompt_file_after_timeout(self, tmp_path: Path) -> None:
        log_file = tmp_path / "address.log"
        provider_error = subprocess.TimeoutExpired(["agent"], 12, output="partial")

        with (
            patch.object(core, "_build_address_fix_prompt", return_value="prompt"),
            patch.object(core, "_invoke_address_fix_session", side_effect=provider_error),
            pytest.raises(RuntimeError, match="Fix session timed out for PR Hephaestus#42"),
        ):
            core.run_address_fix_session(
                issue_number=1,
                pr_number=42,
                worktree_path=tmp_path,
                threads=[_thread()],
                agent="claude",
                repo_root=tmp_path,
                parse_fn=MagicMock(),
                log_file=log_file,
            )

        assert log_file.read_text() == "TIMEOUT after 12s\n\nOutput:\npartial"
        assert not (tmp_path / ".claude-address-review-1.md").exists()

    @pytest.mark.parametrize(
        ("error", "expected_log", "expected_message"),
        [
            (
                subprocess.CalledProcessError(2, ["agent"], output="stdout", stderr="stderr"),
                "EXIT CODE: 2\n\nSTDOUT:\nstdout\n\nSTDERR:\nstderr",
                "Fix session failed for PR Hephaestus#42: stderr",
            ),
            (
                subprocess.TimeoutExpired(["agent"], 12, output="partial"),
                "TIMEOUT after 12s\n\nOutput:\npartial",
                "Fix session timed out for PR Hephaestus#42",
            ),
        ],
    )
    def test_translates_provider_errors_and_persists_details(
        self,
        tmp_path: Path,
        error: subprocess.CalledProcessError | subprocess.TimeoutExpired,
        expected_log: str,
        expected_message: str,
    ) -> None:
        with patch.object(core, "_persist_address_fix_log") as persist:
            with pytest.raises(RuntimeError, match=expected_message):
                core._raise_address_fix_error(
                    error,
                    log_file=tmp_path / "address.log",
                    pr_number=42,
                )

        persist.assert_called_once_with(tmp_path / "address.log", expected_log)
