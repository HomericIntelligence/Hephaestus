"""Tests for hephaestus.validation.coverage."""

import builtins
import json
from pathlib import Path
from typing import Any

import pytest

from hephaestus.validation.coverage import (
    CoverageDataAbsentError,
    CoverageParserUnavailableError,
    CoverageReportParseError,
    check_coverage,
    get_module_threshold,
    load_coverage_config,
    main,
    parse_coverage_report,
)


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    """Create a minimal coverage config without per-module floors."""
    config_file = tmp_path / "coverage.toml"
    config_file.write_text("[coverage]\nminimum = 80\n")
    return config_file


class TestLoadCoverageConfig:
    """Tests for load_coverage_config()."""

    def test_default_config_when_missing(self, tmp_path: Path) -> None:
        """Returns default config when file does not exist."""
        config = load_coverage_config(tmp_path / "nonexistent.toml")
        assert config["coverage"]["target"] == 90.0
        assert config["coverage"]["minimum"] == 80.0

    def test_loads_toml_file(self, tmp_path: Path) -> None:
        """Loads config from a valid TOML file."""
        config_file = tmp_path / "coverage.toml"
        config_file.write_text("[coverage]\ntarget = 95.0\nminimum = 85.0\n")
        config = load_coverage_config(config_file)
        assert config["coverage"]["target"] == 95.0
        assert config["coverage"]["minimum"] == 85.0

    def test_invalid_toml_returns_default(self, tmp_path: Path) -> None:
        """Invalid TOML returns default config."""
        config_file = tmp_path / "coverage.toml"
        config_file.write_text("this is not valid toml {{{}}")
        config = load_coverage_config(config_file)
        assert config["coverage"]["target"] == 90.0

    def test_none_uses_default(self) -> None:
        """None config_file returns default config."""
        config = load_coverage_config(None)
        assert "coverage" in config


class TestGetModuleThreshold:
    """Tests for get_module_threshold()."""

    def test_exact_match(self) -> None:
        """Exact path match returns module-specific threshold."""
        config = {
            "coverage": {
                "minimum": 80.0,
                "modules": {"mypackage/core": {"minimum": 95.0}},
            }
        }
        assert get_module_threshold("mypackage/core", config) == 95.0

    def test_prefix_match(self) -> None:
        """Prefix path match returns parent module threshold."""
        config = {
            "coverage": {
                "minimum": 80.0,
                "modules": {"mypackage": {"minimum": 90.0}},
            }
        }
        assert get_module_threshold("mypackage/sub", config) == 90.0

    def test_fallback_to_default(self) -> None:
        """Unknown path falls back to overall minimum."""
        config = {"coverage": {"minimum": 75.0, "modules": {}}}
        assert get_module_threshold("unknown/path", config) == 75.0

    def test_no_modules_section(self) -> None:
        """Missing modules section uses overall minimum."""
        config = {"coverage": {"minimum": 70.0}}
        assert get_module_threshold("any/path", config) == 70.0


class TestParseCoverageReport:
    """Tests for parse_coverage_report()."""

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """Missing files are distinct from parsed reports with absent data."""
        with pytest.raises(FileNotFoundError, match="Coverage file not found"):
            parse_coverage_report(tmp_path / "coverage.xml")

    def test_parses_cobertura_xml(self, tmp_path: Path) -> None:
        """Parses line-rate from Cobertura XML."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n'
            '<coverage version="7.4" line-rate="0.85" branch-rate="0">\n'
            "</coverage>\n"
        )
        result = parse_coverage_report(coverage_xml)
        assert result is not None
        assert abs(result - 85.0) < 0.01

    def test_no_line_rate_raises_absent_data(self, tmp_path: Path) -> None:
        """A parsed report without line-rate raises the absent-data error."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text('<?xml version="1.0" ?>\n<coverage version="7.4"></coverage>\n')

        with pytest.raises(CoverageDataAbsentError, match="no root line-rate"):
            parse_coverage_report(coverage_xml)

    def test_malformed_xml_raises_parse_error(self, tmp_path: Path) -> None:
        """Malformed XML raises the secure parse error."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text("this is not xml")

        with pytest.raises(CoverageReportParseError, match="securely parse"):
            parse_coverage_report(coverage_xml)

    def test_missing_parser_raises_actionable_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unavailable secure parser identifies the required extra."""
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text('<coverage line-rate="0.90"></coverage>\n')
        real_import = builtins.__import__

        def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("defusedxml"):
                raise ImportError("simulated missing defusedxml")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)

        with pytest.raises(
            CoverageParserUnavailableError,
            match=r"HomericIntelligence-Hephaestus\[xml\]",
        ):
            parse_coverage_report(coverage_xml)


class TestCheckCoverage:
    """Tests for check_coverage()."""

    def test_coverage_above_threshold(self, tmp_path: Path) -> None:
        """Coverage above threshold passes."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.90"></coverage>\n'
        )
        result = check_coverage(80.0, "mypackage/", coverage_xml)
        assert result is True

    def test_coverage_below_threshold(self, tmp_path: Path) -> None:
        """Coverage below threshold fails."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.50"></coverage>\n'
        )
        result = check_coverage(80.0, "mypackage/", coverage_xml)
        assert result is False

    def test_missing_coverage_file_fails(self, tmp_path: Path) -> None:
        """Missing coverage files fail closed."""
        result = check_coverage(80.0, "mypackage/", tmp_path / "missing.xml")
        assert result is False


class TestMain:
    """Tests for main() CLI entry point."""

    def test_missing_coverage_file_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Missing coverage file exits 1."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--path",
                "pkg/",
                "--coverage-file",
                str(tmp_path / "missing.xml"),
            ],
        )
        assert main() == 1

    def test_with_threshold_flag(self, tmp_path: Path, monkeypatch, empty_config: Path) -> None:
        """Explicit threshold flag works."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.90"></coverage>\n'
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--path",
                "pkg/",
                "--coverage-file",
                str(coverage_xml),
                "--config",
                str(empty_config),
            ],
        )
        assert main() == 0

    def test_verbose_flag(self, tmp_path: Path, monkeypatch, empty_config: Path) -> None:
        """Verbose flag works."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.90"></coverage>\n'
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--path",
                "pkg/",
                "--coverage-file",
                str(coverage_xml),
                "--verbose",
                "--config",
                str(empty_config),
            ],
        )
        assert main() == 0

    def test_json_missing_coverage_file(self, tmp_path: Path, monkeypatch, capsys) -> None:
        """--json emits an error envelope when the coverage file is missing."""
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--path",
                "pkg/",
                "--coverage-file",
                str(tmp_path / "missing.xml"),
                "--json",
            ],
        )
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["passed"] is False
        assert payload["coverage"] is None
        assert payload["error"] == "coverage_file_missing"
        assert "not found" in payload["message"]

    def test_json_passing(self, tmp_path: Path, monkeypatch, capsys, empty_config: Path) -> None:
        """--json emits a structured payload when coverage passes."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.95"></coverage>\n'
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--path",
                "pkg/",
                "--coverage-file",
                str(coverage_xml),
                "--json",
                "--config",
                str(empty_config),
            ],
        )
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["threshold"] == 80
        assert payload["coverage"] >= 80

    def test_json_failing(self, tmp_path: Path, monkeypatch, capsys, empty_config: Path) -> None:
        """--json returns 1 and reports failure when below threshold."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text(
            '<?xml version="1.0" ?>\n<coverage version="7.4" line-rate="0.50"></coverage>\n'
        )
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--path",
                "pkg/",
                "--coverage-file",
                str(coverage_xml),
                "--json",
                "--config",
                str(empty_config),
            ],
        )
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False

    def test_json_unparseable_coverage(
        self, tmp_path: Path, monkeypatch, capsys, empty_config: Path
    ) -> None:
        """--json fails with actionable context for malformed XML."""
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text("not xml at all")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--path",
                "pkg/",
                "--coverage-file",
                str(coverage_xml),
                "--json",
                "--config",
                str(empty_config),
            ],
        )
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False
        assert payload["coverage"] is None
        assert payload["error"] == "report_unparseable"
        assert str(coverage_xml) in payload["message"]

    def test_unparseable_coverage_returns_one(
        self, tmp_path: Path, monkeypatch, capsys, empty_config: Path
    ) -> None:
        """Human-readable mode fails explicitly for malformed XML."""
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text("not xml at all")
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--coverage-file",
                str(coverage_xml),
                "--config",
                str(empty_config),
            ],
        )

        assert main() == 1
        assert "Could not securely parse coverage report" in capsys.readouterr().err

    def test_json_missing_parser(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        empty_config: Path,
    ) -> None:
        """--json identifies an unavailable secure parser."""
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text('<coverage line-rate="0.90"></coverage>\n')
        real_import = builtins.__import__

        def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.startswith("defusedxml"):
                raise ImportError("simulated missing defusedxml")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", guarded_import)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--coverage-file",
                str(coverage_xml),
                "--json",
                "--config",
                str(empty_config),
            ],
        )

        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False
        assert payload["coverage"] is None
        assert payload["error"] == "parser_unavailable"
        assert "[xml]" in payload["message"]

    def test_json_missing_coverage_data(
        self, tmp_path: Path, monkeypatch, capsys, empty_config: Path
    ) -> None:
        """--json identifies reports without aggregate line-rate data."""
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text('<coverage version="7.4"></coverage>\n')
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--threshold",
                "80",
                "--coverage-file",
                str(coverage_xml),
                "--json",
                "--config",
                str(empty_config),
            ],
        )

        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is False
        assert payload["coverage"] is None
        assert payload["error"] == "coverage_data_absent"
        assert "line-rate" in payload["message"]
