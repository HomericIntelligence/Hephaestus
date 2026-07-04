"""Tests for hephaestus.validation.coverage."""

import json
from pathlib import Path

import pytest

from hephaestus.utils.helpers import get_repo_root
from hephaestus.validation import coverage

check_coverage = coverage.check_coverage
get_module_threshold = coverage.get_module_threshold
load_coverage_config = coverage.load_coverage_config
main = coverage.main
parse_coverage_report = coverage.parse_coverage_report


@pytest.fixture
def empty_config(tmp_path: Path) -> Path:
    """Create a minimal coverage config without per-module floors."""
    config_file = tmp_path / "coverage.toml"
    config_file.write_text("[coverage]\nminimum = 80\n")
    return config_file


def write_omit_fixture(tmp_path: Path) -> Path:
    """Create a synthetic repo with justified and unjustified coverage omits."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.coverage.run]\nomit = [\n"
        '  "*/tests/*",\n'
        '  "hephaestus/automation/backed.py",\n'
        '  "hephaestus/automation/no_tests.py",\n'
        '  "hephaestus/automation/unbacked.py",\n'
        "]\n"
    )
    automation_dir = tmp_path / "hephaestus" / "automation"
    automation_dir.mkdir(parents=True)
    for module in ("backed", "no_tests", "unbacked"):
        (automation_dir / f"{module}.py").write_text("\n", encoding="utf-8")
    test_dir = tmp_path / "tests" / "unit" / "automation"
    test_dir.mkdir(parents=True)
    (test_dir / "test_backed.py").write_text(
        "from hephaestus.automation import backed\n\n"
        "def test_backed_helper() -> None:\n"
        "    assert backed is not None\n",
        encoding="utf-8",
    )
    (test_dir / "test_no_tests.py").write_text(
        "from hephaestus.automation import no_tests\n\nVALUE = no_tests\n",
        encoding="utf-8",
    )
    return tmp_path


def write_justified_omit_fixture(tmp_path: Path) -> Path:
    """Create a synthetic repo whose coverage omit has a backing unit test."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nomit = [\n  "hephaestus/automation/backed.py",\n]\n',
        encoding="utf-8",
    )
    automation_dir = tmp_path / "hephaestus" / "automation"
    automation_dir.mkdir(parents=True)
    (automation_dir / "backed.py").write_text("\n", encoding="utf-8")
    test_dir = tmp_path / "tests" / "unit" / "automation"
    test_dir.mkdir(parents=True)
    (test_dir / "test_backed.py").write_text(
        "from hephaestus.automation import backed\n\n"
        "def test_backed_helper() -> None:\n"
        "    assert backed is not None\n",
        encoding="utf-8",
    )
    return tmp_path


def write_package_omit_fixture(tmp_path: Path) -> Path:
    """Create a synthetic repo whose omitted automation module is a package."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nomit = [\n  "hephaestus/automation/package_mod/*.py",\n]\n',
        encoding="utf-8",
    )
    package_dir = tmp_path / "hephaestus" / "automation" / "package_mod"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("\n", encoding="utf-8")
    (package_dir / "calls.py").write_text("\n", encoding="utf-8")
    test_dir = tmp_path / "tests" / "unit" / "automation"
    test_dir.mkdir(parents=True)
    (test_dir / "test_package_mod.py").write_text(
        "from hephaestus.automation import package_mod\n\n"
        "def test_package_mod_helper() -> None:\n"
        "    assert package_mod is not None\n",
        encoding="utf-8",
    )
    return tmp_path


def write_stale_omit_fixture(tmp_path: Path) -> Path:
    """Create a synthetic repo with an omit entry for a missing module."""
    (tmp_path / "pyproject.toml").write_text(
        '[tool.coverage.run]\nomit = [\n  "hephaestus/automation/removed.py",\n]\n',
        encoding="utf-8",
    )
    test_dir = tmp_path / "tests" / "unit" / "automation"
    test_dir.mkdir(parents=True)
    (test_dir / "test_removed.py").write_text(
        "from hephaestus.automation import removed\n\n"
        "def test_removed_helper() -> None:\n"
        "    assert removed is not None\n",
        encoding="utf-8",
    )
    return tmp_path


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


class TestCoverageOmitJustifications:
    """Tests for the executable coverage omit-list justification guard."""

    def test_finds_unjustified_automation_omits(self, tmp_path: Path) -> None:
        """Reports omitted automation modules without backing unit tests."""
        assert hasattr(coverage, "find_unjustified_coverage_omits")
        repo_root = write_omit_fixture(tmp_path)

        unjustified = coverage.find_unjustified_coverage_omits(repo_root)

        assert unjustified == ["no_tests", "unbacked"]

    def test_omit_guard_passes_real_repo(self) -> None:
        """The shipped omit list has backing tests for every omitted module."""
        assert coverage.find_unjustified_coverage_omits(get_repo_root()) == []

    def test_finds_stale_automation_omits(self, tmp_path: Path) -> None:
        """Reports omit-list entries that no longer exist in the source tree."""
        repo_root = write_stale_omit_fixture(tmp_path)

        assert coverage.find_stale_coverage_omits(repo_root) == ["removed"]

    def test_omit_guard_has_no_stale_entries_in_real_repo(self) -> None:
        """The shipped omit list points only at existing automation modules."""
        assert coverage.find_stale_coverage_omits(get_repo_root()) == []

    def test_package_omit_glob_maps_to_module_name(self, tmp_path: Path) -> None:
        """A package omit glob is justified by tests for the package module."""
        repo_root = write_package_omit_fixture(tmp_path)

        assert coverage.find_stale_coverage_omits(repo_root) == []
        assert coverage.find_unjustified_coverage_omits(repo_root) == []

    def test_cli_omit_guard_returns_one_for_unjustified_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI mode blocks an omitted module with no backing unit-test suite."""
        repo_root = write_omit_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--check-omit-justification",
                "--repo-root",
                str(repo_root),
            ],
        )

        assert main() == 1
        captured = capsys.readouterr()
        assert "no_tests" in captured.err
        assert "unbacked" in captured.err

    def test_cli_omit_guard_json_reports_unjustified_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI mode emits machine-readable failure details."""
        repo_root = write_omit_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--check-omit-justification",
                "--repo-root",
                str(repo_root),
                "--json",
            ],
        )

        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["missing_modules"] == []
        assert payload["unjustified_modules"] == ["no_tests", "unbacked"]

    def test_cli_omit_guard_json_reports_missing_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI mode emits machine-readable real-tree mismatch details."""
        repo_root = write_stale_omit_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--check-omit-justification",
                "--repo-root",
                str(repo_root),
                "--json",
            ],
        )

        assert main() == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "error"
        assert payload["missing_modules"] == ["removed"]
        assert payload["unjustified_modules"] == []

    def test_cli_omit_guard_text_reports_missing_modules(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The text CLI mode names stale omit entries for operators."""
        repo_root = write_stale_omit_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--check-omit-justification",
                "--repo-root",
                str(repo_root),
            ],
        )

        assert main() == 1
        captured = capsys.readouterr()
        assert "no longer match source files" in captured.err
        assert "removed" in captured.err

    def test_cli_omit_guard_uses_default_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The CLI mode resolves repo root when --repo-root is omitted."""
        repo_root = write_justified_omit_fixture(tmp_path)
        monkeypatch.setattr(coverage, "get_repo_root", lambda: repo_root)
        monkeypatch.setattr("sys.argv", ["check-coverage", "--check-omit-justification"])

        assert main() == 0
        assert "Coverage omit-list justification OK." in capsys.readouterr().out

    def test_cli_omit_guard_json_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The successful CLI JSON path emits a machine-readable status."""
        repo_root = write_justified_omit_fixture(tmp_path)
        monkeypatch.setattr(
            "sys.argv",
            [
                "check-coverage",
                "--check-omit-justification",
                "--repo-root",
                str(repo_root),
                "--json",
            ],
        )

        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "ok"

    def test_precommit_runs_omit_guard_for_pyproject_changes(self) -> None:
        """Changing coverage omit entries runs the executable guard."""
        config = Path(".pre-commit-config.yaml").read_text(encoding="utf-8")

        hook_block = config.split("id: hephaestus-check-coverage-omit-justification", 1)[1].split(
            "\n  - repo:", 1
        )[0]
        assert "hephaestus-check-coverage --check-omit-justification" in hook_block
        assert "PYTHONPATH=." in hook_block
        assert "pyproject\\.toml" in hook_block
        assert "hephaestus/automation/.*\\.py" in hook_block
        assert "tests/unit/automation" in hook_block


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
        import json

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
        assert "not found" in payload["message"]

    def test_json_passing(self, tmp_path: Path, monkeypatch, capsys, empty_config: Path) -> None:
        """--json emits a structured payload when coverage passes."""
        import json

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
        import json

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
        """--json returns 0 with passed=True when coverage is unparseable."""
        import json

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
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["passed"] is True
        assert payload["coverage"] is None
