"""Tests for hephaestus.validation.coverage."""

from pathlib import Path

import pytest

from hephaestus.validation.coverage import (
    check_coverage,
    get_module_threshold,
    load_coverage_config,
    main,
    parse_coverage_report,
)


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

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing file returns None."""
        result = parse_coverage_report(tmp_path / "coverage.xml")
        assert result is None

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

    def test_no_line_rate(self, tmp_path: Path) -> None:
        """XML without line-rate returns None."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text('<?xml version="1.0" ?>\n<coverage version="7.4"></coverage>\n')
        result = parse_coverage_report(coverage_xml)
        assert result is None

    def test_malformed_xml(self, tmp_path: Path) -> None:
        """Malformed XML returns None."""
        pytest.importorskip("defusedxml")
        coverage_xml = tmp_path / "coverage.xml"
        coverage_xml.write_text("this is not xml")
        result = parse_coverage_report(coverage_xml)
        assert result is None


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

    def test_missing_coverage_file_passes(self, tmp_path: Path) -> None:
        """Missing coverage file passes gracefully."""
        result = check_coverage(80.0, "mypackage/", tmp_path / "missing.xml")
        assert result is True


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

    def test_with_threshold_flag(self, tmp_path: Path, monkeypatch) -> None:
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
            ],
        )
        assert main() == 0

    def test_verbose_flag(self, tmp_path: Path, monkeypatch) -> None:
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
            ],
        )
        assert main() == 0
