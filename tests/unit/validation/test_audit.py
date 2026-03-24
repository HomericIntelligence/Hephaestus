"""Tests for hephaestus.validation.audit."""

import io
import json
from pathlib import Path

from hephaestus.validation.audit import (
    extract_cvss_score,
    filter_audit_results,
    load_ignore_list,
    main,
    severity_label,
)


class TestLoadIgnoreList:
    """Tests for load_ignore_list()."""

    def test_loads_ids(self, tmp_path: Path) -> None:
        """Reads IDs from file, ignoring comments and blanks."""
        ignore_file = tmp_path / ".pip-audit-ignore.txt"
        ignore_file.write_text("# Comment\nGHSA-abc-123\n\nGHSA-def-456\n")
        result = load_ignore_list(ignore_file)
        assert result == frozenset({"GHSA-abc-123", "GHSA-def-456"})

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Missing file returns empty frozenset."""
        result = load_ignore_list(tmp_path / "nonexistent.txt")
        assert result == frozenset()

    def test_inline_comments_stripped(self, tmp_path: Path) -> None:
        """Inline comments after IDs are stripped."""
        ignore_file = tmp_path / ".pip-audit-ignore.txt"
        ignore_file.write_text("GHSA-abc-123  # known false positive\n")
        result = load_ignore_list(ignore_file)
        assert result == frozenset({"GHSA-abc-123"})

    def test_empty_file(self, tmp_path: Path) -> None:
        """Empty file returns empty frozenset."""
        ignore_file = tmp_path / ".pip-audit-ignore.txt"
        ignore_file.write_text("")
        result = load_ignore_list(ignore_file)
        assert result == frozenset()


class TestExtractCvssScore:
    """Tests for extract_cvss_score()."""

    def test_numeric_score(self) -> None:
        """Extracts numeric score from severity entry."""
        result = extract_cvss_score([{"score": 7.5}])
        assert result == 7.5

    def test_string_numeric_score(self) -> None:
        """Extracts score from base_score field."""
        result = extract_cvss_score([{"base_score": "9.1"}])
        assert result == 9.1

    def test_no_score_returns_none(self) -> None:
        """Returns None when no numeric score is available."""
        result = extract_cvss_score([{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}])
        assert result is None

    def test_empty_list(self) -> None:
        """Empty severity list returns None."""
        assert extract_cvss_score([]) is None

    def test_highest_score_selected(self) -> None:
        """When multiple scores exist, returns the highest."""
        result = extract_cvss_score([{"score": 3.0}, {"score": 8.5}])
        assert result == 8.5


class TestSeverityLabel:
    """Tests for severity_label()."""

    def test_critical(self) -> None:
        assert severity_label(9.5) == "CRITICAL"

    def test_high(self) -> None:
        assert severity_label(7.5) == "HIGH"

    def test_medium(self) -> None:
        assert severity_label(5.0) == "MEDIUM"

    def test_low(self) -> None:
        assert severity_label(2.0) == "LOW"

    def test_none_score(self) -> None:
        assert severity_label(0.0) == "NONE"

    def test_unknown(self) -> None:
        assert severity_label(None) == "UNKNOWN"

    def test_boundary_critical(self) -> None:
        assert severity_label(9.0) == "CRITICAL"

    def test_boundary_high(self) -> None:
        assert severity_label(7.0) == "HIGH"


class TestFilterAuditResults:
    """Tests for filter_audit_results()."""

    def _make_data(self, vulns: list[dict], name: str = "pkg", version: str = "1.0") -> dict:
        return {"dependencies": [{"name": name, "version": version, "vulns": vulns}]}

    def test_high_severity_blocks(self) -> None:
        """HIGH severity vulnerabilities are blocking."""
        data = self._make_data([{"id": "CVE-1", "severity": [{"score": 8.0}]}])
        blocking, suppressed = filter_audit_results(data)
        assert len(blocking) == 1
        assert blocking[0][2] == "CVE-1"
        assert len(suppressed) == 0

    def test_low_severity_suppressed(self) -> None:
        """LOW severity vulnerabilities are suppressed."""
        data = self._make_data([{"id": "CVE-2", "severity": [{"score": 3.0}]}])
        blocking, suppressed = filter_audit_results(data)
        assert len(blocking) == 0
        assert len(suppressed) == 1

    def test_ignored_ids_skipped(self) -> None:
        """Ignored vulnerability IDs are completely skipped."""
        data = self._make_data([{"id": "CVE-SKIP", "severity": [{"score": 9.5}]}])
        blocking, suppressed = filter_audit_results(data, ignore_ids=frozenset({"CVE-SKIP"}))
        assert len(blocking) == 0
        assert len(suppressed) == 0

    def test_no_vulnerabilities(self) -> None:
        """No vulnerabilities returns empty lists."""
        data = {"dependencies": [{"name": "safe", "version": "1.0", "vulns": []}]}
        blocking, suppressed = filter_audit_results(data)
        assert blocking == []
        assert suppressed == []

    def test_custom_threshold(self) -> None:
        """Custom threshold changes what is blocking."""
        data = self._make_data([{"id": "CVE-3", "severity": [{"score": 5.0}]}])
        blocking, _suppressed = filter_audit_results(data, threshold=4.0)
        assert len(blocking) == 1

    def test_no_score_is_suppressed(self) -> None:
        """Vulnerabilities with no CVSS score are suppressed, not blocking."""
        data = self._make_data([{"id": "CVE-4", "severity": []}])
        blocking, suppressed = filter_audit_results(data)
        assert len(blocking) == 0
        assert len(suppressed) == 1


class TestMain:
    """Tests for main() CLI entry point."""

    def test_no_json_input(self, monkeypatch) -> None:
        """No JSON on stdin returns 0."""
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO("No known vulnerabilities found"))
        assert main() == 0

    def test_invalid_json(self, monkeypatch) -> None:
        """Invalid JSON returns 1."""
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO("{invalid json"))
        assert main() == 1

    def test_clean_audit(self, monkeypatch) -> None:
        """Clean audit with no vulns returns 0."""
        data = {"dependencies": [{"name": "safe", "version": "1.0", "vulns": []}]}
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 0

    def test_blocking_vuln(self, monkeypatch) -> None:
        """Blocking vulnerability returns 1."""
        data = {
            "dependencies": [
                {
                    "name": "bad",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-1", "severity": [{"score": 9.5}]}],
                }
            ]
        }
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 1

    def test_suppressed_only(self, monkeypatch) -> None:
        """Only suppressed vulns returns 0."""
        data = {
            "dependencies": [
                {
                    "name": "pkg",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-2", "severity": [{"score": 3.0}]}],
                }
            ]
        }
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 0
