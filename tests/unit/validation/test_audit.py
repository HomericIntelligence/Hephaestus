"""Tests for hephaestus.validation.audit."""

import io
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from hephaestus.validation import audit as audit_module
from hephaestus.validation.audit import (
    _load_suppression_ledger,
    _parse_ledger,
    _resolve_ledger_path,
    extract_cvss_score,
    filter_audit_results,
    main,
    severity_label,
)

TODAY = date(2026, 8, 7)

VALID_RECORD = (
    "advisory=GHSA-abcd-2345-6789 | package=example-package | "
    "version=1.2.3 | owner=@security-owner | review=issue:#2566 | "
    "expires=2099-12-31 | rationale=Temporary upstream compatibility"
)


def _write_ledger(tmp_path: Path, content: str) -> Path:
    """Write a suppression ledger into a temporary repository root."""
    ledger = tmp_path / ".pip-audit-ignore.txt"
    ledger.write_text(content, encoding="utf-8")
    return ledger


def _evidence(
    *vulns: dict[str, object],
    name: str = "example-package",
    version: str = "1.2.3",
) -> dict[str, object]:
    """Build one structurally valid pip-audit document."""
    return {
        "dependencies": [{"name": name, "version": version, "vulns": list(vulns)}],
        "fixes": [],
    }


def _scan(
    monkeypatch: pytest.MonkeyPatch,
    ledger: Path,
    *,
    returncode: int,
    evidence: object,
    json_mode: bool = False,
) -> int:
    """Run the canonical scan with an injected ledger and scanner result."""
    stdout = evidence if isinstance(evidence, str) else json.dumps(evidence)
    monkeypatch.setattr(
        audit_module,
        "_resolve_ledger_path",
        lambda root=None: ledger,
    )
    monkeypatch.setattr(
        audit_module,
        "_run_pip_audit",
        lambda: subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=""
        ),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["filter-audit", "--scan", *(["--json"] if json_mode else [])],
    )
    return main()


class TestLedgerGrammar:
    """Tests for the structured suppression ledger grammar."""

    def test_valid_record_parses_every_field(self, tmp_path: Path) -> None:
        """A complete record exposes its validated fields."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        (record,) = _load_suppression_ledger(ledger, today=TODAY)
        assert record.advisory == "GHSA-abcd-2345-6789"
        assert record.package == "example-package"
        assert record.version == "1.2.3"
        assert record.owner == "@security-owner"
        assert record.review == "issue:#2566"
        assert record.expires == date(2099, 12, 31)
        assert record.rationale == "Temporary upstream compatibility"
        assert record.key == ("GHSA-abcd-2345-6789", "example-package", "1.2.3")

    def test_blank_lines_and_full_line_comments_are_allowed(self) -> None:
        """Blank lines and comments whose first token is '#' carry no record."""
        content = f"\n# a comment\n   # an indented comment\n\n{VALID_RECORD}\n"
        records = _parse_ledger(content, today=TODAY)
        assert len(records) == 1

    def test_empty_ledger_has_no_records(self) -> None:
        """An empty ledger approves nothing."""
        assert _parse_ledger("", today=TODAY) == ()

    @pytest.mark.parametrize(
        "record",
        [
            pytest.param("GHSA-abcd-2345-6789", id="bare-id"),
            pytest.param(
                VALID_RECORD.replace("owner=@security-owner | ", ""),
                id="missing-field",
            ),
            pytest.param(
                VALID_RECORD.replace("review=issue:#2566", "review=url:https://example.test"),
                id="unknown-review",
            ),
            pytest.param(
                VALID_RECORD.replace("expires=2099-12-31", "expires=not-a-date"),
                id="invalid-date",
            ),
            pytest.param(
                VALID_RECORD.replace("package=example-package", "package=Example_Package"),
                id="noncanonical-package",
            ),
            pytest.param(
                VALID_RECORD.replace("version=1.2.3", "version=01.2.3"),
                id="noncanonical-version",
            ),
            pytest.param(
                VALID_RECORD.replace("owner=@security-owner", "owner=security-owner"),
                id="owner-without-at",
            ),
            pytest.param(
                VALID_RECORD.replace("advisory=GHSA-abcd-2345-6789", "advisory=ghsa-abcd"),
                id="lowercase-advisory",
            ),
            pytest.param(
                VALID_RECORD.replace(
                    "rationale=Temporary upstream compatibility",
                    "rationale=",
                ),
                id="empty-rationale",
            ),
            pytest.param(VALID_RECORD + " | unknown=value", id="extra-field"),
            pytest.param(
                VALID_RECORD.replace(" | rationale=", " | rationale=contains | delimiter "),
                id="delimiter-in-value",
            ),
            pytest.param(VALID_RECORD + "  ", id="trailing-whitespace"),
            pytest.param(
                VALID_RECORD.replace("owner=@security-owner", "owner= @security-owner"),
                id="leading-value-whitespace",
            ),
        ],
    )
    def test_invalid_suppression_records_fail_closed(self, tmp_path: Path, record: str) -> None:
        """Every malformed record stops the ledger with a ValueError."""
        ledger = _write_ledger(tmp_path, record + "\n")
        with pytest.raises(ValueError):
            _load_suppression_ledger(ledger, today=TODAY)

    def test_repeated_field_name_is_rejected(self) -> None:
        """A repeated field name breaks the fixed field order."""
        record = VALID_RECORD.replace("package=example-package", "advisory=GHSA-abcd-2345-6789")
        with pytest.raises(ValueError, match="expected field 'package'"):
            _parse_ledger(record, today=TODAY)

    @pytest.mark.parametrize(
        ("expires", "accepted"),
        [
            pytest.param("2026-08-06", False, id="yesterday"),
            pytest.param("2026-08-07", True, id="today"),
            pytest.param("2026-08-08", True, id="tomorrow"),
        ],
    )
    def test_expiry_is_active_through_the_current_date(self, expires: str, accepted: bool) -> None:
        """A record stops applying the day after it expires."""
        record = VALID_RECORD.replace("expires=2099-12-31", f"expires={expires}")
        if accepted:
            assert len(_parse_ledger(record, today=TODAY)) == 1
        else:
            with pytest.raises(ValueError, match="must not be in the past"):
                _parse_ledger(record, today=TODAY)

    def test_rationale_length_boundaries(self) -> None:
        """A rationale holds one to two hundred characters."""
        for length, accepted in ((1, True), (200, True), (201, False)):
            record = VALID_RECORD.replace(
                "rationale=Temporary upstream compatibility",
                f"rationale={'x' * length}",
            )
            if accepted:
                assert len(_parse_ledger(record, today=TODAY)) == 1
            else:
                with pytest.raises(ValueError, match="1 to 200"):
                    _parse_ledger(record, today=TODAY)

    def test_duplicate_triples_are_rejected(self) -> None:
        """Two records cannot suppress the same finding key."""
        content = f"{VALID_RECORD}\n{VALID_RECORD}\n"
        with pytest.raises(ValueError, match="duplicate record"):
            _parse_ledger(content, today=TODAY)

    def test_distinct_triples_are_allowed(self) -> None:
        """Different versions of one advisory are distinct records."""
        second = VALID_RECORD.replace("version=1.2.3", "version=1.2.4")
        assert len(_parse_ledger(f"{VALID_RECORD}\n{second}\n", today=TODAY)) == 2

    def test_error_names_the_offending_line(self) -> None:
        """A rejected record reports its source line number."""
        content = f"# comment\n\n{VALID_RECORD} | extra=value\n"
        with pytest.raises(ValueError, match=r"\.pip-audit-ignore\.txt:3:"):
            _parse_ledger(content, today=TODAY)


class TestLedgerPathResolution:
    """Tests for canonical ledger path resolution."""

    def test_existing_file_is_returned(self, tmp_path: Path) -> None:
        """The exact ledger file resolves to itself."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        assert _resolve_ledger_path(tmp_path) == ledger

    def test_missing_ledger_is_rejected(self, tmp_path: Path) -> None:
        """A scan without a ledger cannot produce a verdict."""
        with pytest.raises(ValueError, match="does not exist"):
            _resolve_ledger_path(tmp_path)

    def test_directory_is_rejected(self, tmp_path: Path) -> None:
        """A directory named like the ledger is not a ledger."""
        (tmp_path / ".pip-audit-ignore.txt").mkdir()
        with pytest.raises(ValueError, match="does not exist"):
            _resolve_ledger_path(tmp_path)

    def test_symlinked_ledger_is_rejected(self, tmp_path: Path) -> None:
        """A symlink cannot redirect the suppression authority."""
        target = tmp_path / "ledger.txt"
        target.write_text(VALID_RECORD + "\n", encoding="utf-8")
        os.symlink(target, tmp_path / ".pip-audit-ignore.txt")
        with pytest.raises(ValueError, match="symlink"):
            _resolve_ledger_path(tmp_path)


class TestScanSubprocess:
    """Tests for the canonical scanner invocation."""

    def test_exact_command_and_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The scanner runs the pinned argv with the fixed timeout."""
        captured: dict[str, object] = {}

        def _fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            captured["command"] = command
            captured.update(kwargs)
            return subprocess.CompletedProcess(args=command, returncode=0, stdout="{}", stderr="")

        monkeypatch.setattr(subprocess, "run", _fake_run)
        audit_module._run_pip_audit()

        assert captured["command"] == [
            sys.executable,
            "-m",
            "pip_audit",
            "--format",
            "json",
            "--progress-spinner",
            "off",
        ]
        assert captured["timeout"] == 300
        assert captured["capture_output"] is True
        assert captured["text"] is True
        assert captured["check"] is False

    def test_launch_failure_is_operational(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A scanner that cannot start exits with the operational code."""
        ledger = _write_ledger(tmp_path, "")
        monkeypatch.setattr(audit_module, "_resolve_ledger_path", lambda root=None: ledger)

        def _raise() -> subprocess.CompletedProcess[str]:
            raise OSError("no such interpreter")

        monkeypatch.setattr(audit_module, "_run_pip_audit", _raise)
        monkeypatch.setattr("sys.argv", ["filter-audit", "--scan"])
        assert main() == 2

    def test_timeout_is_operational(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """A scanner timeout exits with the operational code."""
        ledger = _write_ledger(tmp_path, "")
        monkeypatch.setattr(audit_module, "_resolve_ledger_path", lambda root=None: ledger)

        def _timeout() -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="pip-audit", timeout=300)

        monkeypatch.setattr(audit_module, "_run_pip_audit", _timeout)
        monkeypatch.setattr("sys.argv", ["filter-audit", "--scan"])
        assert main() == 2

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    def test_malformed_ledger_blocks_the_subprocess(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        json_mode: bool,
    ) -> None:
        """Ledger validation runs before the scanner starts."""
        ledger = _write_ledger(tmp_path, "GHSA-abcd-2345-6789\n")
        calls: list[int] = []
        monkeypatch.setattr(audit_module, "_resolve_ledger_path", lambda root=None: ledger)
        monkeypatch.setattr(audit_module, "_run_pip_audit", lambda: calls.append(1))
        monkeypatch.setattr(
            "sys.argv",
            ["filter-audit", "--scan", *(["--json"] if json_mode else [])],
        )

        assert main() == 2
        assert calls == []

    @pytest.mark.parametrize(
        ("returncode", "vulns", "expected"),
        [
            pytest.param(0, (), 0, id="clean"),
            pytest.param(
                1, ({"id": "GHSA-aaaa-bbbb-cccc", "severity": [{"score": 9.5}]},), 1, id="blocking"
            ),
            pytest.param(0, ({"id": "GHSA-aaaa-bbbb-cccc"},), 2, id="zero-with-findings"),
            pytest.param(1, (), 2, id="one-without-findings"),
            pytest.param(3, (), 2, id="unexpected-status"),
        ],
    )
    def test_status_and_evidence_must_agree(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        returncode: int,
        vulns: tuple[dict[str, object], ...],
        expected: int,
    ) -> None:
        """The scanner status must match the evidence it returned."""
        ledger = _write_ledger(tmp_path, "")
        result = _scan(
            monkeypatch,
            ledger,
            returncode=returncode,
            evidence=_evidence(*vulns),
        )
        assert result == expected

    @pytest.mark.parametrize(
        "evidence",
        [
            pytest.param("", id="empty"),
            pytest.param("No known vulnerabilities found", id="non-json"),
            pytest.param("{invalid json", id="malformed-json"),
            pytest.param(
                {
                    "dependencies": [{"name": "example-package", "skip_reason": ""}],
                    "fixes": [],
                },
                id="blank-skip-reason",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {
                            "name": "example-package",
                            "version": "1.2.3",
                            "skip_reason": "not auditable",
                            "vulns": [],
                        }
                    ],
                    "fixes": [],
                },
                id="skipped-and-audited",
            ),
            pytest.param(
                {
                    "dependencies": [
                        {"name": "example-package", "version": "not-a-version", "vulns": []}
                    ],
                    "fixes": [],
                },
                id="invalid-version",
            ),
        ],
    )
    def test_incomplete_evidence_is_operational(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        evidence: dict[str, object] | str,
    ) -> None:
        """Incomplete scanner evidence cannot produce a clean verdict."""
        ledger = _write_ledger(tmp_path, "")
        assert _scan(monkeypatch, ledger, returncode=0, evidence=evidence) == 2


class TestSkippedDependencies:
    """Tests for dependencies the scanner could not audit."""

    def test_skipped_dependency_is_reported_and_does_not_block(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """An unaudited dependency is named, not treated as a finding."""
        ledger = _write_ledger(tmp_path, "")
        evidence = {
            "dependencies": [{"name": "local-package", "skip_reason": "not on PyPI"}],
            "fixes": [],
        }

        assert _scan(monkeypatch, ledger, returncode=0, evidence=evidence, json_mode=True) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["skipped_dependencies"] == ["local-package"]
        assert report["blocking"] == []

    def test_skipped_dependency_is_listed_in_human_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """Human output names every dependency the scanner skipped."""
        ledger = _write_ledger(tmp_path, "")
        evidence = {
            "dependencies": [{"name": "local-package", "skip_reason": "not on PyPI"}],
            "fixes": [],
        }

        assert _scan(monkeypatch, ledger, returncode=0, evidence=evidence) == 0
        assert "local-package" in capsys.readouterr().out


class TestSuppressionMatching:
    """Tests for exact suppression matching in scan mode."""

    def test_matching_record_approves_the_finding(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """An exact record moves a blocking finding into approved suppressions."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        evidence = _evidence({"id": "GHSA-abcd-2345-6789", "severity": [{"score": 9.5}]})

        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence, json_mode=True) == 0
        report = json.loads(capsys.readouterr().out)
        assert report["blocking"] == []
        assert report["approved_suppressions"] == [
            {
                "package": "example-package",
                "version": "1.2.3",
                "id": "GHSA-abcd-2345-6789",
                "severity": "CRITICAL",
            }
        ]

    def test_package_name_is_normalized_before_matching(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Scanner and ledger names compare in PEP 503 form."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        evidence = _evidence(
            {"id": "GHSA-abcd-2345-6789", "severity": [{"score": 9.5}]},
            name="example-package",
            version="1.2.3",
        )
        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence) == 0

    def test_stale_record_is_operational(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A record that matches no current finding stops the scan."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        assert _scan(monkeypatch, ledger, returncode=0, evidence=_evidence()) == 2

    def test_different_version_does_not_match(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A record suppresses one exact package version only."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        evidence = _evidence(
            {"id": "GHSA-abcd-2345-6789", "severity": [{"score": 9.5}]},
            version="1.2.4",
        )
        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence) == 2

    def test_ambiguous_match_is_operational(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """A record cannot suppress two identical findings."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        vulnerability = {"id": "GHSA-abcd-2345-6789", "severity": [{"score": 9.5}]}
        evidence = {
            "dependencies": [
                {"name": "example-package", "version": "1.2.3", "vulns": [vulnerability]},
                {"name": "example-package", "version": "1.2.3", "vulns": [vulnerability]},
            ],
            "fixes": [],
        }
        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence) == 2

    def test_unsuppressed_blocking_finding_survives(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """An approved record does not hide a second blocking finding."""
        ledger = _write_ledger(tmp_path, VALID_RECORD + "\n")
        evidence = _evidence(
            {"id": "GHSA-abcd-2345-6789", "severity": [{"score": 9.5}]},
            {"id": "GHSA-zzzz-yyyy-xxxx", "severity": [{"score": 8.0}]},
        )

        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence, json_mode=True) == 1
        report = json.loads(capsys.readouterr().out)
        assert [entry["id"] for entry in report["blocking"]] == ["GHSA-zzzz-yyyy-xxxx"]
        assert [entry["id"] for entry in report["approved_suppressions"]] == ["GHSA-abcd-2345-6789"]


class TestJsonOutput:
    """Tests for identical verdict semantics in both output modes."""

    @pytest.mark.parametrize(
        ("returncode", "vulns", "expected"),
        [
            pytest.param(0, (), 0, id="clean"),
            pytest.param(
                1, ({"id": "GHSA-aaaa-bbbb-cccc", "severity": [{"score": 2.0}]},), 0, id="warning"
            ),
            pytest.param(
                1, ({"id": "GHSA-aaaa-bbbb-cccc", "severity": [{"score": 8.0}]},), 1, id="high"
            ),
            pytest.param(
                1, ({"id": "GHSA-aaaa-bbbb-cccc", "severity": [{"score": 9.5}]},), 1, id="critical"
            ),
            pytest.param(1, ({"id": "GHSA-aaaa-bbbb-cccc"},), 1, id="unknown"),
        ],
    )
    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    def test_severity_verdict_is_identical_in_both_modes(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        returncode: int,
        vulns: tuple[dict[str, object], ...],
        expected: int,
        json_mode: bool,
    ) -> None:
        """The same evidence yields the same exit code in either output mode."""
        ledger = _write_ledger(tmp_path, "")
        result = _scan(
            monkeypatch,
            ledger,
            returncode=returncode,
            evidence=_evidence(*vulns),
            json_mode=json_mode,
        )
        assert result == expected

    def test_json_report_lists_separate_collections(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        """The JSON report keeps blocking, warnings, and approvals apart."""
        ledger = _write_ledger(tmp_path, "")
        evidence = {
            "dependencies": [
                {
                    "name": "example-package",
                    "version": "1.2.3",
                    "vulns": [
                        {"id": "GHSA-aaaa-bbbb-cccc", "severity": [{"score": 3.0}]},
                        {"id": "GHSA-dddd-eeee-ffff", "severity": [{"score": 8.1}]},
                    ],
                }
            ],
            "fixes": [],
        }

        assert _scan(monkeypatch, ledger, returncode=1, evidence=evidence, json_mode=True) == 1
        report = json.loads(capsys.readouterr().out)
        assert [entry["id"] for entry in report["warnings"]] == ["GHSA-aaaa-bbbb-cccc"]
        assert [entry["id"] for entry in report["blocking"]] == ["GHSA-dddd-eeee-ffff"]
        assert report["approved_suppressions"] == []
        assert report["exit_code"] == 1


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
        return {"dependencies": [{"name": name, "version": version, "vulns": vulns}], "fixes": []}

    def test_high_severity_blocks(self) -> None:
        """HIGH severity vulnerabilities are blocking."""
        data = self._make_data([{"id": "CVE-1", "severity": [{"score": 8.0}]}])
        blocking, warnings = filter_audit_results(data)
        assert len(blocking) == 1
        assert blocking[0][2] == "CVE-1"
        assert len(warnings) == 0

    def test_low_severity_is_a_warning(self) -> None:
        """LOW severity vulnerabilities do not block."""
        data = self._make_data([{"id": "CVE-2", "severity": [{"score": 3.0}]}])
        blocking, warnings = filter_audit_results(data)
        assert len(blocking) == 0
        assert len(warnings) == 1

    def test_no_advisory_id_bypass(self) -> None:
        """The public filter has no caller-controlled suppression input."""
        data = self._make_data([{"id": "CVE-SKIP", "severity": [{"score": 9.5}]}])
        with pytest.raises(TypeError):
            filter_audit_results(data, ignore_ids=frozenset({"CVE-SKIP"}))  # type: ignore[call-arg]

    def test_no_vulnerabilities(self) -> None:
        """No vulnerabilities returns empty lists."""
        data = {"dependencies": [{"name": "safe", "version": "1.0", "vulns": []}], "fixes": []}
        blocking, warnings = filter_audit_results(data)
        assert blocking == []
        assert warnings == []

    def test_custom_threshold(self) -> None:
        """Custom threshold changes what is blocking."""
        data = self._make_data([{"id": "CVE-3", "severity": [{"score": 5.0}]}])
        blocking, _warnings = filter_audit_results(data, threshold=4.0)
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
        blocking, warnings = filter_audit_results(self._make_data([vulnerability]))
        assert blocking == [("pkg", "1.0", "CVE-4", "UNKNOWN")]
        assert warnings == []

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"dependencies": [{}], "fixes": []}, id="empty-dependency"),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0"}], "fixes": []},
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
                    ],
                    "fixes": [],
                },
                id="missing-vulnerability-id",
            ),
        ],
    )
    def test_invalid_nested_result_raises(self, data: object) -> None:
        """Malformed nested scanner data cannot produce a clean verdict."""
        with pytest.raises(ValueError, match=r"dependencies\[0\]"):
            filter_audit_results(data)

    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"dependencies": []}, id="missing-fixes"),
            pytest.param({"dependencies": [], "fixes": {}}, id="fixes-not-list"),
        ],
    )
    def test_fixes_list_is_required(self, data: object) -> None:
        """Incomplete scanner evidence without a fixes list cannot pass."""
        with pytest.raises(ValueError, match="fixes must be a list"):
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
        blocking, warnings = filter_audit_results(data)
        assert blocking == [("pkg", "1.0", "CVE-5", "CRITICAL")]
        assert warnings == []


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
        """Missing or unparsable evidence exits with the operational code."""
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(raw))

        assert main() == 2
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 2
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
            pytest.param({"dependencies": []}, id="missing-fixes"),
            pytest.param({"dependencies": [], "fixes": {}}, id="fixes-not-list"),
        ],
    )
    def test_invalid_top_level_shape_fails_closed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        data: object,
        json_mode: bool,
    ) -> None:
        """Only an object containing dependencies and fixes lists is accepted."""
        argv = ["filter-audit", *(["--json"] if json_mode else [])]
        monkeypatch.setattr("sys.argv", argv)
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))

        assert main() == 2
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 2
        else:
            assert "invalid pip-audit evidence" in captured.err

    @pytest.mark.parametrize("json_mode", [False, True], ids=["human", "json"])
    @pytest.mark.parametrize(
        "data",
        [
            pytest.param({"dependencies": [None], "fixes": []}, id="dependency-not-object"),
            pytest.param({"dependencies": [{}], "fixes": []}, id="empty-dependency"),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0"}], "fixes": []},
                id="missing-vulns",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "skip_reason": 1}], "fixes": []},
                id="invalid-skip-reason",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": {}}], "fixes": []},
                id="vulns-not-list",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [None]}], "fixes": []},
                id="vulnerability-not-object",
            ),
            pytest.param(
                {"dependencies": [{"name": "pkg", "version": "1.0", "vulns": [{}]}], "fixes": []},
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
                    ],
                    "fixes": [],
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
                    ],
                    "fixes": [],
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

        assert main() == 2
        captured = capsys.readouterr()
        if json_mode:
            report = json.loads(captured.out)
            assert report["status"] == "error"
            assert report["exit_code"] == 2
        else:
            assert "invalid pip-audit evidence" in captured.err

    def test_clean_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clean audit with no vulns returns 0."""
        data = {"dependencies": [{"name": "safe", "version": "1.0", "vulns": []}], "fixes": []}
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 0

    def test_blocking_vuln(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Blocking vulnerability returns 1."""
        data = {
            "dependencies": [
                {
                    "name": "bad",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-1", "severity": [{"score": 9.5}]}],
                }
            ],
            "fixes": [],
        }
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 1

    def test_warning_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Only below-threshold vulns returns 0."""
        data = {
            "dependencies": [
                {
                    "name": "pkg",
                    "version": "1.0",
                    "vulns": [{"id": "CVE-2", "severity": [{"score": 3.0}]}],
                }
            ],
            "fixes": [],
        }
        monkeypatch.setattr("sys.argv", ["filter-audit"])
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(data)))
        assert main() == 0

    @pytest.mark.parametrize("option", ["--ignore-file", "--ignore-vuln"])
    def test_retired_parser_options_are_rejected(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        option: str,
    ) -> None:
        """No caller can redirect the suppression authority from the CLI."""
        monkeypatch.setattr("sys.argv", ["filter-audit", option, "value"])

        assert main() == 2
        assert "unrecognized arguments" in capsys.readouterr().err

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
            ],
            "fixes": [],
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
