"""Unit tests for hephaestus.automation.audit_reviewer."""

import json
import logging
import subprocess
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import given, strategies as st

from hephaestus.automation._review_utils import DEFAULT_STATE_DIR
from hephaestus.automation.audit_reviewer import (
    AuditReviewer,
    _build_coordinator_prompt,
    _build_parser,
    _fetch_prs_by_number,
    _parse_coordinator_results,
    main,
    print_audit_summary,
    run_audit_coordinator,
    write_audit_report,
)


class TestParseCoordinatorResults:
    """Test JSON fence extraction and aggregation."""

    def test_empty_string(self) -> None:
        assert _parse_coordinator_results("") == []

    def test_whitespace_only(self) -> None:
        assert _parse_coordinator_results("   \n\n  ") == []

    def test_prose_no_fence(self) -> None:
        text = "This PR looks good to me but I have some concerns."
        assert _parse_coordinator_results(text) == []

    def test_single_audits_block(self) -> None:
        text = """```json
{"audits": [{"pr_number": 100, "verdict": "GO"}]}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["pr_number"] == 100
        assert result[0]["verdict"] == "GO"

    def test_multi_audits_block(self) -> None:
        text = """```json
{"audits": [
  {"pr_number": 100, "verdict": "GO"},
  {"pr_number": 101, "verdict": "NOGO"}
]}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 2
        assert result[0]["pr_number"] == 100
        assert result[1]["pr_number"] == 101

    def test_single_dict_no_wrapper(self) -> None:
        text = """```json
{"pr_number": 100, "verdict": "UNSURE"}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["pr_number"] == 100

    def test_extra_fields_preserved(self) -> None:
        text = """```json
{"audits": [
  {"pr_number": 100, "verdict": "GO", "summary": "Looks good", "custom": "value"}
]}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["custom"] == "value"
        assert result[0]["summary"] == "Looks good"

    def test_malformed_fence_skipped(self) -> None:
        text = """```json
{"invalid": "json}
```
```json
{"audits": [{"pr_number": 100, "verdict": "GO"}]}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["pr_number"] == 100

    def test_mixed_valid_and_invalid_blocks(self) -> None:
        text = """```json
{"pr_number": 100, "verdict": "GO"}
```
Some text
```json
{"pr_number": 101, "verdict": "NOGO"}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 2

    def test_crlf_line_endings_inside_fence(self) -> None:
        text = '```json\r\n{"audits": [{"pr_number": 100, "verdict": "GO"}]}\r\n```'
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["pr_number"] == 100

    def test_non_dict_entries_in_audits_filtered(self) -> None:
        text = """```json
{"audits": ["not_a_dict", {"pr_number": 100, "verdict": "GO"}]}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 1
        assert result[0]["pr_number"] == 100


class TestWriteAuditReport:
    """Test report persistence."""

    def test_writes_valid_json_to_state_dir(self, tmp_path: Path) -> None:
        audits = [{"pr_number": 100, "verdict": "GO"}]
        report = write_audit_report(tmp_path, audits)
        assert report.exists()
        data = json.loads(report.read_text())
        assert data["audits"] == audits
        assert "generated_at" in data

    def test_timestamp_in_filename_utc(self, tmp_path: Path) -> None:
        audits: list[dict[str, Any]] = []
        report = write_audit_report(tmp_path, audits)
        assert "audit-report-" in report.name
        assert ".json" in report.name
        assert "Z" in report.name

    def test_creates_state_dir_if_missing(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "nested" / "dir"
        assert not state_dir.exists()
        audits: list[dict[str, Any]] = []
        report = write_audit_report(state_dir, audits)
        assert state_dir.exists()
        assert report.parent == state_dir

    def test_preserves_extra_audit_fields(self, tmp_path: Path) -> None:
        audits = [
            {
                "pr_number": 100,
                "verdict": "GO",
                "summary": "Good",
                "findings": ["a", "b"],
            }
        ]
        report = write_audit_report(tmp_path, audits)
        data = json.loads(report.read_text())
        assert data["audits"][0]["summary"] == "Good"
        assert data["audits"][0]["findings"] == ["a", "b"]


class TestPrintAuditSummary:
    """Test log output formatting."""

    def test_one_log_line_per_audit(self, caplog: pytest.LogCaptureFixture) -> None:
        audits = [
            {"pr_number": 100, "verdict": "GO", "summary": "Looks good"},
            {"pr_number": 101, "verdict": "NOGO", "summary": "Needs work"},
        ]
        with caplog.at_level(logging.INFO):
            print_audit_summary(audits)
        assert len(caplog.records) == 2
        assert "#100" in caplog.text
        assert "GO" in caplog.text
        assert "#101" in caplog.text
        assert "NOGO" in caplog.text

    def test_missing_verdict_defaults_to_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        audits = [{"pr_number": 100}]
        with caplog.at_level(logging.INFO):
            print_audit_summary(audits)
        assert "UNKNOWN" in caplog.text

    def test_summary_truncated_to_120_chars(self, caplog: pytest.LogCaptureFixture) -> None:
        long_summary = "x" * 200
        audits = [{"pr_number": 100, "verdict": "GO", "summary": long_summary}]
        with caplog.at_level(logging.INFO):
            print_audit_summary(audits)
        assert len(caplog.records[0].message) < len(long_summary)


class TestBuildCoordinatorPrompt:
    """Test prompt construction."""

    def test_prompt_mentions_each_pr_number(self) -> None:
        prs = [
            {"number": 100, "title": "Feature A", "url": "http://..."},
            {"number": 101, "title": "Feature B", "url": "http://..."},
        ]
        prompt = _build_coordinator_prompt(prs)
        assert "#100" in prompt
        assert "#101" in prompt

    def test_prompt_forbids_backgrounding(self) -> None:
        prs = [{"number": 100, "title": "Test", "url": "http://..."}]
        prompt = _build_coordinator_prompt(prs)
        assert "background" in prompt.lower()

    def test_prompt_says_execute_not_plan(self) -> None:
        prs = [{"number": 100, "title": "Test", "url": "http://..."}]
        prompt = _build_coordinator_prompt(prs)
        assert "EXECUTE" in prompt
        assert "plan" in prompt.lower()


class TestFetchPrsByNumber:
    """Test PR number resolution."""

    def test_empty_list_returns_empty(self) -> None:
        result = _fetch_prs_by_number([])
        assert result == []

    @mock.patch("hephaestus.automation.audit_reviewer._gh_call")
    def test_each_number_resolved_independently(self, mock_gh: mock.Mock) -> None:
        mock_gh.return_value = mock.Mock(stdout=json.dumps({"number": 100, "title": "Test"}))
        result = _fetch_prs_by_number([100, 101])
        assert len(result) == 2
        assert mock_gh.call_count == 2

    @mock.patch("hephaestus.automation.audit_reviewer._gh_call")
    def test_single_failure_logs_warning_does_not_abort(self, mock_gh: mock.Mock) -> None:
        mock_gh.side_effect = [
            mock.Mock(stdout=json.dumps({"number": 100, "title": "Test"})),
            Exception("Network error"),
        ]
        result = _fetch_prs_by_number([100, 101])
        assert len(result) == 1


class TestRunAuditCoordinator:
    """Test coordinator invocation."""

    def test_dry_run_returns_placeholders(self, tmp_path: Path) -> None:
        prs = [{"number": 100, "title": "Test"}]
        result = run_audit_coordinator(prs=prs, agent="claude", state_dir=tmp_path, dry_run=True)
        assert len(result) == 1
        assert result[0]["verdict"] == "UNSURE"

    def test_empty_prs_short_circuits(self, tmp_path: Path) -> None:
        result = run_audit_coordinator(prs=[], agent="claude", state_dir=tmp_path)
        assert result == []

    @mock.patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    def test_claude_path_invokes_session(
        self,
        mock_slug: mock.Mock,
        mock_root: mock.Mock,
        mock_invoke: mock.Mock,
        tmp_path: Path,
    ) -> None:
        prs = [{"number": 100, "title": "Test"}]
        mock_root.return_value = Path(".")
        mock_slug.return_value = "test/repo"
        mock_invoke.return_value = (
            '```json\n{"audits": [{"pr_number": 100, "verdict": "GO"}]}\n```',
            "",
        )
        run_audit_coordinator(prs=prs, agent="claude", state_dir=tmp_path)
        assert mock_invoke.called

    @mock.patch("hephaestus.automation.audit_reviewer.run_agent_text")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_root")
    def test_codex_path_invokes_runtime(
        self,
        mock_root: mock.Mock,
        mock_agent: mock.Mock,
        tmp_path: Path,
    ) -> None:
        prs = [{"number": 100, "title": "Test"}]
        mock_root.return_value = Path(".")
        mock_agent.return_value = mock.Mock(
            stdout='```json\n{"audits": [{"pr_number": 100, "verdict": "GO"}]}\n```'
        )
        run_audit_coordinator(prs=prs, agent="codex", state_dir=tmp_path)
        assert mock_agent.call_args.kwargs["agent"] == "codex"

    @mock.patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    def test_subprocess_error_raises_runtime_error(
        self,
        mock_slug: mock.Mock,
        mock_root: mock.Mock,
        mock_invoke: mock.Mock,
        tmp_path: Path,
    ) -> None:
        prs = [{"number": 100, "title": "Test"}]
        mock_root.return_value = Path(".")
        mock_slug.return_value = "test/repo"
        mock_invoke.side_effect = subprocess.CalledProcessError(1, "cmd")
        with pytest.raises(RuntimeError):
            run_audit_coordinator(prs=prs, agent="claude", state_dir=tmp_path)

    @mock.patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    def test_timeout_raises_runtime_error(
        self,
        mock_slug: mock.Mock,
        mock_root: mock.Mock,
        mock_invoke: mock.Mock,
        tmp_path: Path,
    ) -> None:
        prs = [{"number": 100, "title": "Test"}]
        mock_root.return_value = Path(".")
        mock_slug.return_value = "test/repo"
        mock_invoke.side_effect = subprocess.TimeoutExpired("cmd", 60)
        with pytest.raises(RuntimeError):
            run_audit_coordinator(prs=prs, agent="claude", state_dir=tmp_path)

    @mock.patch("hephaestus.automation.audit_reviewer.invoke_claude_with_session")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_root")
    @mock.patch("hephaestus.automation.audit_reviewer.get_repo_slug")
    def test_nonempty_response_zero_parses_raises(
        self,
        mock_slug: mock.Mock,
        mock_root: mock.Mock,
        mock_invoke: mock.Mock,
        tmp_path: Path,
    ) -> None:
        prs = [{"number": 100, "title": "Test"}]
        mock_root.return_value = Path(".")
        mock_slug.return_value = "test/repo"
        mock_invoke.return_value = ("Some prose with no JSON blocks", "")
        with pytest.raises(RuntimeError, match="no parseable JSON"):
            run_audit_coordinator(prs=prs, agent="claude", state_dir=tmp_path)


class TestAuditReviewerRun:
    """Test AuditReviewer.run() orchestration."""

    @mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs")
    @mock.patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @mock.patch("hephaestus.automation.audit_reviewer.gh_pr_review_post")
    def test_happy_path_posts_summary_review(
        self, mock_post: mock.Mock, mock_coord: mock.Mock, mock_fetch: mock.Mock
    ) -> None:
        mock_fetch.return_value = [{"number": 100, "title": "Test"}]
        mock_coord.return_value = [{"pr_number": 100, "verdict": "GO", "summary": "Good"}]
        reviewer = AuditReviewer()
        rc, audits = reviewer.run()
        assert rc == 0
        assert len(audits) == 1
        assert mock_post.called

    def test_post_init_always_sets_state_dir(self, tmp_path: Path) -> None:
        """#1426: ``__post_init__`` guarantees ``state_dir`` is non-None.

        This invariant is what makes the ``if self.state_dir is None`` guard in
        ``run()`` a genuinely-unreachable mypy type-narrowing branch (kept
        behind ``# pragma: no cover - mypy type-narrowing; unreachable``).
        """
        # An explicitly-provided value is preserved verbatim.
        explicit = AuditReviewer(state_dir=tmp_path)
        assert explicit.state_dir == tmp_path

        # The default path is populated via ensure_state_dir(get_repo_root()).
        with mock.patch(
            "hephaestus.automation.audit_reviewer.ensure_state_dir",
            return_value=tmp_path / "state",
        ):
            default = AuditReviewer()
        assert default.state_dir is not None

    @mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs")
    def test_no_prs_returns_zero(self, mock_fetch: mock.Mock) -> None:
        mock_fetch.return_value = []
        reviewer = AuditReviewer()
        rc, audits = reviewer.run()
        assert rc == 0
        assert audits == []

    @mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs")
    @mock.patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    def test_coordinator_failure_returns_one(
        self, mock_coord: mock.Mock, mock_fetch: mock.Mock
    ) -> None:
        mock_fetch.return_value = [{"number": 100, "title": "Test"}]
        mock_coord.side_effect = RuntimeError("Coordinator failed")
        reviewer = AuditReviewer()
        rc, audits = reviewer.run()
        assert rc == 1
        assert audits == []

    @mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs")
    @mock.patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    @mock.patch("hephaestus.automation.audit_reviewer.gh_pr_review_post")
    def test_posting_failure_logged_run_continues(
        self, mock_post: mock.Mock, mock_coord: mock.Mock, mock_fetch: mock.Mock
    ) -> None:
        mock_fetch.return_value = [{"number": 100, "title": "Test"}]
        mock_coord.return_value = [{"pr_number": 100, "verdict": "GO", "summary": "Good"}]
        mock_post.side_effect = Exception("GitHub error")
        reviewer = AuditReviewer()
        rc, audits = reviewer.run()
        assert rc == 0
        assert len(audits) == 1

    @mock.patch("hephaestus.automation.audit_reviewer._fetch_prs_by_number")
    @mock.patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    def test_pr_numbers_path_uses_fetch_prs_by_number(
        self, mock_coord: mock.Mock, mock_fetch_nums: mock.Mock
    ) -> None:
        mock_fetch_nums.return_value = [{"number": 100, "title": "Test"}]
        mock_coord.return_value = []
        reviewer = AuditReviewer(pr_numbers=[100])
        _, _ = reviewer.run()
        assert mock_fetch_nums.called

    @mock.patch("hephaestus.automation.audit_reviewer.gh_pr_review_post")
    @mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs")
    @mock.patch("hephaestus.automation.audit_reviewer.run_audit_coordinator")
    def test_dry_run_passes_dry_run_to_gh_pr_review_post(
        self, mock_coord: mock.Mock, mock_fetch: mock.Mock, mock_post: mock.Mock
    ) -> None:
        mock_fetch.return_value = [{"number": 100, "title": "Test"}]
        mock_coord.return_value = [{"pr_number": 100, "verdict": "GO", "summary": "Good"}]
        reviewer = AuditReviewer(dry_run=True)
        _, _ = reviewer.run()
        assert mock_coord.call_args[1]["dry_run"] is True
        mock_post.assert_called_once()
        assert mock_post.call_args[1]["dry_run"] is True

    def test_state_dir_default_under_build(self, tmp_path: Path) -> None:
        with mock.patch(
            "hephaestus.automation.audit_reviewer.get_repo_root",
            return_value=tmp_path,
        ):
            reviewer = AuditReviewer()

        assert reviewer.state_dir == tmp_path / DEFAULT_STATE_DIR
        assert reviewer.state_dir.is_dir()


class TestParser:
    """Test CLI argument parsing."""

    def test_default_flags(self) -> None:
        parser = _build_parser()
        args = parser.parse_args([])
        assert args.pr_numbers == []
        assert args.codex is False
        assert args.dry_run is False
        assert args.verbose is False

    def test_pr_numbers_accepts_multiple(self) -> None:
        parser = _build_parser()
        args = parser.parse_args(["--pr-numbers", "100", "101", "102"])
        assert args.pr_numbers == [100, 101, 102]

    def test_codex_flag_sets_agent_to_codex_in_main(self) -> None:
        with mock.patch("hephaestus.automation.audit_reviewer.AuditReviewer") as mock_cls:
            mock_instance = mock.Mock()
            mock_instance.run.return_value = (0, [])
            mock_cls.return_value = mock_instance
            main(["--codex", "--dry-run"])
            assert mock_cls.call_args[1]["agent"] == "codex"

    def test_codex_flag_resolves_codex_for_live_run(self) -> None:
        with mock.patch(
            "hephaestus.automation.audit_reviewer.resolve_agent",
            return_value="codex",
        ) as mock_resolve:
            with mock.patch("hephaestus.automation.audit_reviewer.AuditReviewer") as mock_cls:
                mock_instance = mock.Mock()
                mock_instance.run.return_value = (0, [])
                mock_cls.return_value = mock_instance
                main(["--codex"])
                mock_resolve.assert_called_once_with("codex")
                assert mock_cls.call_args[1]["agent"] == "codex"

    def test_dry_run_skips_resolve_agent(self) -> None:
        with mock.patch("hephaestus.automation.audit_reviewer.resolve_agent") as mock_resolve:
            with mock.patch("hephaestus.automation.audit_reviewer.AuditReviewer") as mock_cls:
                mock_instance = mock.Mock()
                mock_instance.run.return_value = (0, [])
                mock_cls.return_value = mock_instance
                main(["--dry-run"])
                mock_resolve.assert_not_called()
                assert mock_cls.call_args[1]["agent"] == "claude"

    def test_main_configures_cli_logging(self) -> None:
        with mock.patch("hephaestus.automation.audit_reviewer.configure_cli_logging") as configure:
            with mock.patch("hephaestus.automation.audit_reviewer.AuditReviewer") as mock_cls:
                mock_instance = mock.Mock()
                mock_instance.run.return_value = (0, [])
                mock_cls.return_value = mock_instance

                main(["--dry-run", "--verbose"])

        configure.assert_called_once_with(verbose=True)

    def test_json_flag_emits_envelope_on_exit(self) -> None:
        with mock.patch("hephaestus.automation.audit_reviewer.AuditReviewer") as mock_cls:
            mock_instance = mock.Mock()
            mock_instance.run.return_value = (0, [])
            mock_cls.return_value = mock_instance
            with mock.patch("hephaestus.automation.audit_reviewer.emit_json_status"):
                main(["--dry-run", "--json"])

    def test_help_exits_zero(self) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit) as exc_info:
            parser.parse_args(["--help"])
        assert exc_info.value.code == 0


class TestParseCoordinatorResultsProperties:
    """Property-based fuzz coverage for _parse_coordinator_results (#1470)."""

    @given(st.text())
    def test_never_raises_returns_list_of_dicts(self, text: str) -> None:
        result = _parse_coordinator_results(text)
        assert isinstance(result, list)
        assert all(isinstance(item, dict) for item in result)

    @given(st.text())
    def test_no_json_fence_returns_empty(self, text: str) -> None:
        if "```json" not in text:
            assert _parse_coordinator_results(text) == []

    @given(st.text(max_size=200))
    def test_malformed_fence_is_skipped_not_raised(self, junk: str) -> None:
        # A malformed JSON fence must be dropped, never raise (audit_reviewer.py:60).
        body = f"```json\n{junk}\n```"
        assert isinstance(_parse_coordinator_results(body), list)

    @given(st.integers(), st.text(max_size=120))
    def test_wellformed_pr_block_parsed(self, pr_number: int, summary: str) -> None:
        body = f'```json\n{{"pr_number": {pr_number}, "summary": {json.dumps(summary)}}}\n```'
        result = _parse_coordinator_results(body)
        assert result and result[0]["pr_number"] == pr_number
