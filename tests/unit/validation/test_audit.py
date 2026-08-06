"""Tests for hephaestus.validation.audit."""

import io
import json
from pathlib import Path

import pytest

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

    def test_string_score_does_not_skip_base_score(self) -> None:
        """Considers base_score even when score is a numeric string."""
        result = extract_cvss_score([{"score": "5.0", "base_score": "9.1"}])
        assert result == 9.1

    def test_non_finite_score_does_not_skip_base_score(self) -> None:
        """Ignores non-finite score text while preserving a finite base_score."""
        result = extract_cvss_score([{"score": "nan", "base_score": "9.8"}])
        assert result == 9.8

    def test_cvss_vector_score(self) -> None:
        """Computes the base score from a CVSS v3 vector-only entry."""
        result = extract_cvss_score([{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}])
        assert result == 9.8

    def test_cvss_changed_scope_vector_score(self) -> None:
        """Computes the base score for a changed-scope CVSS v3 vector-only entry."""
        result = extract_cvss_score([{"score": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H"}])
        assert result == 9.9

    def test_cvss_30_changed_scope_vector_score(self) -> None:
        """CVSS 3.0 changed-scope vectors use the same Impact formula as 3.1.

        Official base score per the CVSS v3.0 spec is 9.9 — identical to the
        v3.1 score for the same vector.
        """
        result = extract_cvss_score([{"score": "CVSS:3.0/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H"}])
        assert result == 9.9

    def test_cvss_30_changed_scope_high_boundary(self) -> None:
        """A mid-ISS CVSS 3.0 changed-scope vector scores correctly at the HIGH boundary.

        Per the v3.0 base spec: ISS = 1 - (1-0.56)(1-0.22)(1-0.22) = 0.732304,
        Impact = 7.52*(ISS-0.029) - 3.25*(ISS-0.02)^15 = 5.268808,
        Exploitability = 8.22*0.62*0.77*0.5*0.62 = 1.216511,
        Roundup(1.08*(Impact+Expl)) = Roundup(7.004144) = 7.1.
        The former ModifiedImpact-style branch (ISS*0.9731, ^13) yielded 7.0.
        """
        result = extract_cvss_score([{"score": "CVSS:3.0/AV:A/AC:L/PR:H/UI:R/S:C/C:H/I:L/A:L"}])
        assert result == 7.1

    def test_cvss_vector_lowercase_metrics(self) -> None:
        """Lowercase vector text is normalized before scoring."""
        result = extract_cvss_score([{"score": "cvss:3.0/av:n/ac:l/pr:l/ui:n/s:c/c:h/i:h/a:h"}])
        assert result == 9.9

    def test_malformed_vector_returns_none(self) -> None:
        """A truncated CVSS vector yields no score."""
        result = extract_cvss_score([{"score": "CVSS:3.1/AV:N/AC:L"}])
        assert result is None

    def test_out_of_range_numeric_score_ignored(self) -> None:
        """Scores outside the 0.0-10.0 CVSS range are rejected."""
        assert extract_cvss_score([{"score": 10.1}]) is None
        assert extract_cvss_score([{"base_score": "-0.1"}]) is None

    def test_bool_score_ignored(self) -> None:
        """Boolean values are not treated as numeric CVSS scores."""
        assert extract_cvss_score([{"score": True}]) is None

    def test_non_numeric_score_returns_none(self) -> None:
        """Returns None when score text is neither numeric nor a supported CVSS vector."""
        result = extract_cvss_score([{"score": "unknown"}])
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

    def _make_data(
        self,
        vulns: list[dict[str, object]],
        name: str = "pkg",
        version: str = "1.0",
    ) -> dict[str, object]:
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

    @pytest.mark.parametrize(
        "vulnerability",
        [
            pytest.param({"id": "CVE-4"}, id="missing-severity"),
            pytest.param({"id": "CVE-4", "severity": []}, id="empty-severity"),
            pytest.param(
                {"id": "CVE-4", "severity": [{"score": "unknown"}]},
                id="unparseable-score",
            ),
        ],
    )
    def test_unknown_score_is_blocking(self, vulnerability: dict[str, object]) -> None:
        """Structurally valid advisories without a parseable score block."""
        blocking, suppressed = filter_audit_results(self._make_data([vulnerability]))
        assert blocking == [("pkg", "1.0", "CVE-4", "UNKNOWN")]
        assert suppressed == []

    def test_ignored_unscored_id_is_skipped(self) -> None:
        """Only the matching reviewed advisory ID bypasses an unscored finding."""
        data = self._make_data([{"id": "CVE-4"}])
        blocking, suppressed = filter_audit_results(
            data,
            ignore_ids=frozenset({"CVE-4"}),
        )
        assert blocking == []
        assert suppressed == []

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"dependencies": [{}]}, id="empty-dependency"),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0"}]},
                id="missing-vulns",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {
                            "name": "pkg",
                            "version": "1.0",
                            "vulns": [{}],
                        }
                    ]
                },
                id="missing-vulnerability-id",
            ),
        ],
    )
    def test_invalid_nested_result_raises(self, data: object) -> None:
        """Malformed nested scanner data cannot produce a clean verdict."""
        with pytest.raises(ValueError, match=r"dependencies\[0\]"):
            filter_audit_results(data)

    def test_cvss_vector_only_high_severity_blocks(self) -> None:
        """Vector-only HIGH/CRITICAL vulnerabilities are blocking."""
        data = self._make_data(
            [
                {
                    "id": "CVE-5",
                    "severity": [{"score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                }
            ]
        )
        blocking, suppressed = filter_audit_results(data)
        assert blocking == [("pkg", "1.0", "CVE-5", "CRITICAL")]
        assert suppressed == []


class TestMain:
    """Tests for main() CLI entry point."""

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    @pytest.mark.parametrize(
        "raw",
        [
            pytest.param("", id="empty"),
            pytest.param("No known vulnerabilities found", id="non-json"),
            pytest.param("{invalid json", id="malformed-json"),
        ],
    )
    def test_invalid_scanner_output_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        raw: str,
        json_mode: bool,
    ) -> None:
        """Missing or unparsable evidence exits nonzero in both output modes."""
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))

        assert main() == 1
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 1
        else:
            assert "invalid pip-audit evidence" in captured.err

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param(None, id="null"),
            pytest.param("scanner output", id="string"),
            pytest.param(1, id="number"),
            pytest.param(True, id="boolean"),
            pytest.param([], id="array"),
            pytest.param({}, id="missing-dependencies"),
            pytest.param({"dependencies": {}}, id="dependencies-not-list"),
        ],
    )
    def test_invalid_top_level_shape_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        data: object,
        json_mode: bool,
    ) -> None:
        """Only an object containing a dependencies list is accepted."""
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))

        assert main() == 1
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 1
        else:
            assert "invalid pip-audit evidence" in captured.err

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"dependencies": [None]}, id="dependency-not-object"),
            pytest.param({"dependencies": [{}]}, id="empty-dependency"),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0"}]},
                id="missing-vulns",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "skip_reason": "not auditable"}]},
                id="skipped-dependency",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": {}}]},
                id="vulns-not-list",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [None]}]},
                id="vulnerability-not-object",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [{}]}]},
                id="missing-vulnerability-id",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {
                            "name": "pkg",
                            "version": "1.0",
                            "vulns": [{"id": "CVE-1", "severity": {}}],
                        }
                    ]
                },
                id="severity-not-list",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {
                            "name": "pkg",
                            "version": "1.0",
                            "vulns": [{"id": "CVE-1", "severity": [None]}],
                        }
                    ]
                },
                id="severity-entry-not-object",
            ),
        ],
    )
    def test_invalid_nested_shape_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        data: object,
        json_mode: bool,
    ) -> None:
        """Every structurally invalid nested payload uses the shared error path."""
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))

        assert main() == 1
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 1
        else:
            assert "invalid pip-audit evidence" in captured.err

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

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    def test_unscored_vulnerability_blocks_in_all_output_modes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        json_mode: bool,
    ) -> None:
        """Human and JSON output expose the same blocking UNKNOWN verdict."""
        data = {
            "dependencies": [
                {
                    "name": "pkg",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-UNKNOWN"}],
                }
            ]
        }
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))

        assert main() == 1
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["exit_code"] == 1
            assert report["blocking"] == [
                {
                    "package": "pkg",
                    "version": "1.0",
                    "id": "CVE-UNKNOWN",
                    "severity": "UNKNOWN",
                }
            ]
        else:
            assert "[UNKNOWN] pkg==1.0 CVE-UNKNOWN" in captured.out
