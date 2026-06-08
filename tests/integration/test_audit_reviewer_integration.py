"""Integration tests for hephaestus.automation.audit_reviewer."""

import json
from pathlib import Path
from unittest import mock

import pytest

from hephaestus.automation.audit_reviewer import (
    AuditReviewer,
    _parse_coordinator_results,
    print_audit_summary,
    write_audit_report,
)


@pytest.mark.integration
class TestAuditReviewerIntegration:
    """Integration tests with real disk I/O and state verification."""

    def test_module_importable(self) -> None:
        """Verify the audit_reviewer module is importable."""
        from hephaestus.automation.audit_reviewer import AuditReviewer as ImportedReviewer

        assert ImportedReviewer is not None

    def test_audit_reviewer_class_exported_from_package(self) -> None:
        """Verify AuditReviewer is exported from hephaestus.automation."""
        from hephaestus.automation import AuditReviewer as ExportedClass

        assert ExportedClass is AuditReviewer

    def test_parse_coordinator_results_public(self) -> None:
        """Verify _parse_coordinator_results is accessible at module level."""
        assert _parse_coordinator_results is not None
        assert callable(_parse_coordinator_results)

    def test_parse_realistic_multi_pr_output(self) -> None:
        """Test parsing realistic agent output with multiple PR blocks."""
        text = """I have reviewed the open PRs and here are the results:

```json
{"pr_number": 100, "verdict": "GO", "summary": "Looks good", "findings": []}
```

```json
{"pr_number": 101, "verdict": "NOGO", "summary": "Needs work", "findings": ["Test coverage low"]}
```

Overall, we have 2 PRs ready to review."""
        result = _parse_coordinator_results(text)
        assert len(result) == 2
        assert result[0]["pr_number"] == 100
        assert result[0]["verdict"] == "GO"
        assert result[1]["pr_number"] == 101
        assert result[1]["verdict"] == "NOGO"

    def test_parse_multi_block_json(self) -> None:
        """Test parsing a single multi-PR JSON block."""
        text = """```json
{
  "audits": [
    {"pr_number": 100, "verdict": "GO", "summary": "Good", "findings": []},
    {"pr_number": 101, "verdict": "UNSURE", "summary": "Review needed", "findings": ["Complexity"]}
  ]
}
```"""
        result = _parse_coordinator_results(text)
        assert len(result) == 2
        assert result[0]["pr_number"] == 100
        assert result[1]["pr_number"] == 101

    def test_write_audit_report_valid_json(self, tmp_path: Path) -> None:
        """Verify report is valid JSON that can be loaded back."""
        audits = [
            {"pr_number": 100, "verdict": "GO", "summary": "Good"},
            {"pr_number": 101, "verdict": "NOGO", "summary": "Needs work"},
        ]
        report_path = write_audit_report(tmp_path, audits)
        loaded = json.loads(report_path.read_text())
        assert loaded["audits"] == audits
        assert "generated_at" in loaded
        assert len(loaded["audits"]) == 2

    def test_audit_reviewer_run_posting_failure_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Verify posting failure does not prevent run completion."""
        with mock.patch("hephaestus.automation.audit_reviewer.fetch_open_prs") as mock_fetch:
            with mock.patch(
                "hephaestus.automation.audit_reviewer.run_audit_coordinator"
            ) as mock_coord:
                with mock.patch(
                    "hephaestus.automation.audit_reviewer.gh_pr_review_post"
                ) as mock_post:
                    mock_fetch.return_value = [{"number": 100, "title": "Test"}]
                    mock_coord.return_value = [
                        {"pr_number": 100, "verdict": "GO", "summary": "Good"}
                    ]
                    mock_post.side_effect = Exception("GitHub error")
                    reviewer = AuditReviewer(state_dir=tmp_path)
                    rc, audits = reviewer.run()
                    assert rc == 0
                    assert len(audits) == 1
                    assert mock_post.called

    def test_print_audit_summary_per_pr_log_line(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify one log line per PR with key info."""
        import logging

        audits = [
            {"pr_number": 100, "verdict": "GO", "summary": "Excellent work"},
            {"pr_number": 101, "verdict": "NOGO", "summary": "Needs revision"},
        ]
        with caplog.at_level(logging.INFO):
            print_audit_summary(audits)
        lines = [r.message for r in caplog.records]
        assert len(lines) == 2
        assert any("#100" in line and "GO" in line for line in lines)
        assert any("#101" in line and "NOGO" in line for line in lines)

    def test_auditreviewer_construction_default(self) -> None:
        """Verify AuditReviewer initializes with sensible defaults."""
        reviewer = AuditReviewer()
        assert reviewer.agent == "claude"
        assert reviewer.pr_numbers == []
        assert reviewer.dry_run is False
        assert reviewer.state_dir is not None

    def test_auditreviewer_construction_codex(self) -> None:
        """Verify AuditReviewer accepts codex agent."""
        reviewer = AuditReviewer(agent="codex")
        assert reviewer.agent == "codex"

    def test_auditreviewer_construction_pr_numbers_state_dir(self, tmp_path: Path) -> None:
        """Verify AuditReviewer accepts pr_numbers and state_dir."""
        reviewer = AuditReviewer(pr_numbers=[100, 101], state_dir=tmp_path)
        assert reviewer.pr_numbers == [100, 101]
        assert reviewer.state_dir == tmp_path
