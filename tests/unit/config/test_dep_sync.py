"""Tests for hephaestus.config.dep_sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.config.dep_sync import (
    VersionRange,
    _is_deps_section,
    _parse_constraints,
    _parse_version,
    _version_satisfies,
    check_dep_sync,
    check_pyproject_no_deps,
    check_requirements_against_pixi,
    check_requirements_up_to_date,
    generate_requirements_content,
    parse_pixi_toml,
    parse_requirements,
    sync_requirements,
)


class TestParseVersion:
    """Tests for _parse_version()."""

    def test_simple_version(self) -> None:
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_single_component(self) -> None:
        assert _parse_version("2") == (2,)

    def test_two_components(self) -> None:
        assert _parse_version("1.0") == (1, 0)

    def test_with_dash(self) -> None:
        assert _parse_version("1.2-3") == (1, 2, 3)

    def test_ignores_non_digits(self) -> None:
        # Letters between dots are ignored
        assert _parse_version("1.a.3") == (1, 3)

    def test_four_components(self) -> None:
        assert _parse_version("1.2.3.4") == (1, 2, 3, 4)


class TestParseConstraints:
    """Tests for _parse_constraints()."""

    def test_single_gte(self) -> None:
        result = _parse_constraints(">=1.0.0")
        assert result == [VersionRange(op=">=", version=(1, 0, 0))]

    def test_range(self) -> None:
        result = _parse_constraints(">=1.2.0,<2")
        assert len(result) == 2
        assert result[0].op == ">="
        assert result[1].op == "<"

    def test_exact(self) -> None:
        result = _parse_constraints("==1.5.0")
        assert result == [VersionRange(op="==", version=(1, 5, 0))]

    def test_strips_quotes(self) -> None:
        result = _parse_constraints('">=1.0,<2"')
        assert len(result) == 2

    def test_empty_spec(self) -> None:
        result = _parse_constraints("")
        assert result == []

    def test_not_equal(self) -> None:
        result = _parse_constraints("!=1.0.0")
        assert result == [VersionRange(op="!=", version=(1, 0, 0))]


class TestVersionSatisfies:
    """Tests for _version_satisfies()."""

    def test_gte_satisfied(self) -> None:
        constraints = [VersionRange(op=">=", version=(1, 0, 0))]
        assert _version_satisfies((1, 2, 0), constraints) is True

    def test_gte_not_satisfied(self) -> None:
        constraints = [VersionRange(op=">=", version=(2, 0, 0))]
        assert _version_satisfies((1, 9, 9), constraints) is False

    def test_lt_satisfied(self) -> None:
        constraints = [VersionRange(op="<", version=(2,))]
        assert _version_satisfies((1, 9, 9), constraints) is True

    def test_lt_not_satisfied(self) -> None:
        constraints = [VersionRange(op="<", version=(2,))]
        assert _version_satisfies((2, 0, 0), constraints) is False

    def test_range_satisfied(self) -> None:
        constraints = _parse_constraints(">=1.0.0,<2")
        assert _version_satisfies((1, 5, 0), constraints) is True

    def test_range_below(self) -> None:
        constraints = _parse_constraints(">=1.0.0,<2")
        assert _version_satisfies((0, 9, 0), constraints) is False

    def test_range_above(self) -> None:
        constraints = _parse_constraints(">=1.0.0,<2")
        assert _version_satisfies((2, 0, 0), constraints) is False

    def test_exact_match(self) -> None:
        constraints = [VersionRange(op="==", version=(1, 5, 0))]
        assert _version_satisfies((1, 5, 0), constraints) is True

    def test_exact_no_match(self) -> None:
        constraints = [VersionRange(op="==", version=(1, 5, 0))]
        assert _version_satisfies((1, 5, 1), constraints) is False

    def test_not_equal_mismatch(self) -> None:
        constraints = [VersionRange(op="!=", version=(1, 0, 0))]
        assert _version_satisfies((1, 0, 1), constraints) is True

    def test_not_equal_match(self) -> None:
        constraints = [VersionRange(op="!=", version=(1, 0, 0))]
        assert _version_satisfies((1, 0, 0), constraints) is False

    def test_pads_shorter_version(self) -> None:
        # (2,) compared to constraint (2, 0, 0) — should be equal
        constraints = [VersionRange(op="==", version=(2, 0, 0))]
        assert _version_satisfies((2,), constraints) is True

    def test_empty_constraints_always_true(self) -> None:
        assert _version_satisfies((1, 0, 0), []) is True

    def test_unknown_op_treated_as_true(self) -> None:
        constraints = [VersionRange(op="~=", version=(1, 0, 0))]
        assert _version_satisfies((1, 0, 0), constraints) is True


class TestParsePixiToml:
    """Tests for parse_pixi_toml()."""

    def test_reads_dependencies(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[dependencies]\npyyaml = ">=6.0,<7"\npydantic = ">=2.0,<3"\n')
        result = parse_pixi_toml(pixi)
        assert result["pyyaml"] == ">=6.0,<7"
        assert result["pydantic"] == ">=2.0,<3"

    def test_reads_pypi_dependencies(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[pypi-dependencies]\nrequests = ">=2.28,<3"\n')
        result = parse_pixi_toml(pixi)
        assert result["requests"] == ">=2.28,<3"

    def test_ignores_comments(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[dependencies]\n# This is a comment\npyyaml = ">=6.0"\n')
        result = parse_pixi_toml(pixi)
        assert "pyyaml" in result
        assert len(result) == 1

    def test_stops_at_non_dep_section(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[dependencies]\npyyaml = ">=6.0"\n\n[tasks]\nbuild = "mojo build"\n'
        )
        result = parse_pixi_toml(pixi)
        assert "pyyaml" in result
        assert "build" not in result

    def test_reads_feature_dev_dependencies(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[dependencies]\npyyaml = ">=6.0"\n\n[feature.dev.dependencies]\npytest = ">=9.0"\n'
        )
        result = parse_pixi_toml(pixi)
        assert "pyyaml" in result
        assert "pytest" in result

    def test_inline_comment_stripped(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text('[dependencies]\nrequests = ">=2.28" # HTTP client\n')
        result = parse_pixi_toml(pixi)
        assert result["requests"] == ">=2.28"

    def test_empty_file(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text("")
        result = parse_pixi_toml(pixi)
        assert result == {}


class TestParseRequirements:
    """Tests for parse_requirements()."""

    def test_basic_pins(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("pyyaml==6.0.1\npydantic==2.5.0\n")
        result = parse_requirements(req)
        assert result["pyyaml"] == "6.0.1"
        assert result["pydantic"] == "2.5.0"

    def test_skips_comments(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("# comment\npyyaml==6.0.1\n")
        result = parse_requirements(req)
        assert len(result) == 1

    def test_skips_include_lines(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("-r requirements.txt\npyyaml==6.0.1\n")
        result = parse_requirements(req)
        assert len(result) == 1

    def test_lowercases_package_names(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("PyYAML==6.0.1\n")
        result = parse_requirements(req)
        assert "pyyaml" in result

    def test_strips_inline_comment(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("pyyaml==6.0.1  # some comment\n")
        result = parse_requirements(req)
        assert result["pyyaml"] == "6.0.1"

    def test_empty_file(self, tmp_path: Path) -> None:
        req = tmp_path / "requirements.txt"
        req.write_text("")
        result = parse_requirements(req)
        assert result == {}


class TestCheckPyprojectNoDeps:
    """Tests for check_pyproject_no_deps()."""

    def test_no_deps_returns_empty(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project]\nname = 'foo'\n")
        errors = check_pyproject_no_deps(pyproject)
        assert errors == []

    def test_flags_project_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project.dependencies]\nrequests = '>=2'\n")
        errors = check_pyproject_no_deps(pyproject)
        assert any("[project.dependencies]" in e for e in errors)

    def test_flags_optional_dependencies(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("[project.optional-dependencies]\ndev = ['pytest']\n")
        errors = check_pyproject_no_deps(pyproject)
        assert any("[project.optional-dependencies]" in e for e in errors)

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        errors = check_pyproject_no_deps(tmp_path / "nonexistent.toml")
        assert errors == []


class TestCheckRequirementsAgainstPixi:
    """Tests for check_requirements_against_pixi()."""

    def _write_req(self, tmp_path: Path, name: str, content: str) -> None:
        (tmp_path / name).write_text(content)

    def test_valid_pin_passes(self, tmp_path: Path) -> None:
        self._write_req(tmp_path, "requirements.txt", "pyyaml==6.0.1\n")
        errors = check_requirements_against_pixi(tmp_path, {"pyyaml": ">=6.0,<7"})
        assert errors == []

    def test_pin_outside_range_fails(self, tmp_path: Path) -> None:
        self._write_req(tmp_path, "requirements.txt", "pyyaml==7.0.0\n")
        errors = check_requirements_against_pixi(tmp_path, {"pyyaml": ">=6.0,<7"})
        assert len(errors) == 1
        assert "pyyaml" in errors[0]

    def test_pkg_not_in_pixi_fails(self, tmp_path: Path) -> None:
        self._write_req(tmp_path, "requirements.txt", "requests==2.31.0\n")
        errors = check_requirements_against_pixi(tmp_path, {})
        assert len(errors) == 1
        assert "requests" in errors[0]

    def test_skips_missing_files(self, tmp_path: Path) -> None:
        errors = check_requirements_against_pixi(tmp_path, {})
        assert errors == []

    def test_checks_both_req_files(self, tmp_path: Path) -> None:
        self._write_req(tmp_path, "requirements.txt", "pyyaml==6.0.1\n")
        self._write_req(tmp_path, "requirements-dev.txt", "pytest==9.0.0\n")
        errors = check_requirements_against_pixi(
            tmp_path, {"pyyaml": ">=6.0,<7", "pytest": ">=9.0,<10"}
        )
        assert errors == []


class TestCheckDepSync:
    """Tests for check_dep_sync()."""

    def test_no_pixi_toml_returns_error(self, tmp_path: Path) -> None:
        errors = check_dep_sync(tmp_path)
        assert errors == ["pixi.toml not found"]

    def test_clean_state_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "pixi.toml").write_text('[dependencies]\npyyaml = ">=6.0,<7"\n')
        (tmp_path / "requirements.txt").write_text("pyyaml==6.0.1\n")
        errors = check_dep_sync(tmp_path)
        assert errors == []

    def test_out_of_range_pin_fails(self, tmp_path: Path) -> None:
        (tmp_path / "pixi.toml").write_text('[dependencies]\npyyaml = ">=6.0,<7"\n')
        (tmp_path / "requirements.txt").write_text("pyyaml==7.1.0\n")
        errors = check_dep_sync(tmp_path)
        assert len(errors) >= 1


class TestGenerateRequirementsContent:
    """Tests for generate_requirements_content()."""

    def test_basic_output(self) -> None:
        content = generate_requirements_content(["pyyaml"], {"pyyaml": "6.0.1"})
        assert "pyyaml==6.0.1" in content

    def test_includes_header(self) -> None:
        content = generate_requirements_content(["pyyaml"], {"pyyaml": "6.0.1"})
        assert "AUTO-GENERATED" in content

    def test_include_base_prepended(self) -> None:
        content = generate_requirements_content(
            ["pytest"], {"pytest": "9.0.0"}, include_base="-r requirements.txt"
        )
        assert "-r requirements.txt" in content

    def test_section_comments(self) -> None:
        content = generate_requirements_content(
            ["pyyaml"],
            {"pyyaml": "6.0.1"},
            section_comments={"pyyaml": "# YAML parser"},
        )
        assert "# YAML parser" in content

    def test_missing_package_warns(self, capsys: pytest.CaptureFixture) -> None:
        content = generate_requirements_content(["missing-pkg"], {})
        captured = capsys.readouterr()
        assert "WARNING" in captured.err
        assert "missing-pkg" not in content

    def test_ends_with_newline(self) -> None:
        content = generate_requirements_content(["pyyaml"], {"pyyaml": "6.0.1"})
        assert content.endswith("\n")


class TestSyncRequirements:
    """Tests for sync_requirements() and check_requirements_up_to_date()."""

    def test_writes_files(self, tmp_path: Path) -> None:
        paths = sync_requirements(
            tmp_path,
            {"pyyaml": "6.0.1", "pytest": "9.0.0"},
            core_packages=["pyyaml"],
            dev_packages=["pytest"],
        )
        assert len(paths) == 2
        assert (tmp_path / "requirements.txt").exists()
        assert (tmp_path / "requirements-dev.txt").exists()

    def test_core_content(self, tmp_path: Path) -> None:
        sync_requirements(
            tmp_path,
            {"pyyaml": "6.0.1"},
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        content = (tmp_path / "requirements.txt").read_text()
        assert "pyyaml==6.0.1" in content

    def test_dev_includes_base(self, tmp_path: Path) -> None:
        sync_requirements(
            tmp_path,
            {"pyyaml": "6.0.1", "pytest": "9.0.0"},
            core_packages=["pyyaml"],
            dev_packages=["pytest"],
        )
        dev_content = (tmp_path / "requirements-dev.txt").read_text()
        assert "-r requirements.txt" in dev_content

    def test_up_to_date_returns_true(self, tmp_path: Path) -> None:
        sync_requirements(
            tmp_path,
            {"pyyaml": "6.0.1"},
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        ok = check_requirements_up_to_date(
            tmp_path,
            {"pyyaml": "6.0.1"},
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        assert ok is True

    def test_out_of_date_returns_false(self, tmp_path: Path) -> None:
        sync_requirements(
            tmp_path,
            {"pyyaml": "6.0.1"},
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        ok = check_requirements_up_to_date(
            tmp_path,
            {"pyyaml": "6.0.2"},  # different version
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        assert ok is False

    def test_missing_file_returns_false(self, tmp_path: Path) -> None:
        ok = check_requirements_up_to_date(
            tmp_path,
            {"pyyaml": "6.0.1"},
            core_packages=["pyyaml"],
            dev_packages=[],
        )
        assert ok is False


class TestCLIEntryPoints:
    """Tests for check_dep_sync_main() and sync_requirements_main() CLI entry points."""

    def test_check_dep_sync_main_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.config.dep_sync import check_dep_sync_main

        (tmp_path / "pixi.toml").write_text('[dependencies]\npyyaml = ">=6.0,<7"\n')
        monkeypatch.setattr("sys.argv", ["hephaestus-check-dep-sync", "--repo-root", str(tmp_path)])
        assert check_dep_sync_main() == 0

    def test_check_dep_sync_main_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.config.dep_sync import check_dep_sync_main

        (tmp_path / "pixi.toml").write_text('[dependencies]\npyyaml = ">=6.0,<7"\n')
        (tmp_path / "requirements.txt").write_text("pyyaml==8.0.0\n")
        monkeypatch.setattr("sys.argv", ["hephaestus-check-dep-sync", "--repo-root", str(tmp_path)])
        assert check_dep_sync_main() == 1

    def test_sync_requirements_main_check_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.config.dep_sync import sync_requirements_main

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-sync-requirements",
                "--check",
                "--repo-root",
                str(tmp_path),
                "--core",
                "pyyaml",
            ],
        )
        mock_packages = {"pyyaml": "6.0.1"}
        with patch("hephaestus.config.dep_sync.get_pixi_packages", return_value=mock_packages):
            # Files don't exist yet — check mode should return 1
            result = sync_requirements_main()
        assert result == 1

    def test_sync_requirements_main_write_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from hephaestus.config.dep_sync import sync_requirements_main

        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-sync-requirements",
                "--repo-root",
                str(tmp_path),
                "--core",
                "pyyaml",
            ],
        )
        mock_packages = {"pyyaml": "6.0.1"}
        with patch("hephaestus.config.dep_sync.get_pixi_packages", return_value=mock_packages):
            result = sync_requirements_main()
        assert result == 0
        assert (tmp_path / "requirements.txt").exists()


class TestIsDepsSection:
    """Tests for _is_deps_section()."""

    def test_top_level_dependencies(self) -> None:
        assert _is_deps_section("[dependencies]") is True

    def test_top_level_pypi_dependencies(self) -> None:
        assert _is_deps_section("[pypi-dependencies]") is True

    def test_feature_dev_dependencies(self) -> None:
        assert _is_deps_section("[feature.dev.dependencies]") is True

    def test_feature_dev_pypi_dependencies(self) -> None:
        assert _is_deps_section("[feature.dev.pypi-dependencies]") is True

    def test_feature_custom_name_dependencies(self) -> None:
        assert _is_deps_section("[feature.test-tools.dependencies]") is True

    def test_other_section_returns_false(self) -> None:
        assert _is_deps_section("[project]") is False

    def test_feature_without_deps_suffix_returns_false(self) -> None:
        assert _is_deps_section("[feature.dev]") is False

    def test_tasks_section_returns_false(self) -> None:
        assert _is_deps_section("[tasks]") is False


class TestParsePixiTomlFeatureEnvs:
    """Tests for parse_pixi_toml() with [feature.*] sections."""

    def test_reads_feature_dev_dependencies(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[dependencies]\npyyaml = ">=6.0,<7"\n\n'
            '[feature.dev.dependencies]\npytest = ">=9.0,<10"\n'
        )
        result = parse_pixi_toml(pixi)
        assert result["pyyaml"] == ">=6.0,<7"
        assert result["pytest"] == ">=9.0,<10"

    def test_reads_feature_pypi_dependencies(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[pypi-dependencies]\nhomericintelligence-hephaestus = ">=0.7.0"\n\n'
            '[feature.dev.pypi-dependencies]\nmypy = ">=1.19,<2"\n'
        )
        result = parse_pixi_toml(pixi)
        assert result["homericintelligence-hephaestus"] == ">=0.7.0"
        assert result["mypy"] == ">=1.19,<2"

    def test_merges_multiple_feature_envs(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[dependencies]\npyyaml = ">=6.0,<7"\n\n'
            '[feature.dev.dependencies]\npytest = ">=9.0,<10"\n\n'
            '[feature.lint.dependencies]\nruff = ">=0.4,<1"\n'
        )
        result = parse_pixi_toml(pixi)
        assert "pyyaml" in result
        assert "pytest" in result
        assert "ruff" in result

    def test_feature_deps_not_shadowed_by_other_sections(self, tmp_path: Path) -> None:
        pixi = tmp_path / "pixi.toml"
        pixi.write_text(
            '[project]\nname = "foo"\n\n'
            '[feature.dev.dependencies]\npytest = ">=9.0,<10"\n\n'
            '[tasks]\nbuild = "mojo build"\n'
        )
        result = parse_pixi_toml(pixi)
        assert result == {"pytest": ">=9.0,<10"}

    def test_check_dep_sync_finds_feature_dev_packages(self, tmp_path: Path) -> None:
        (tmp_path / "pixi.toml").write_text(
            '[dependencies]\npyyaml = ">=6.0,<7"\n\n'
            '[feature.dev.dependencies]\npytest = ">=9.0,<10"\n'
        )
        (tmp_path / "requirements-dev.txt").write_text("pytest==9.0.3\n")
        errors = check_dep_sync(tmp_path)
        assert errors == []
