"""Tests for hephaestus.validation.python_version."""

from pathlib import Path

import pytest

from hephaestus.validation.python_version import (
    _extract_via_regex,
    check_ci_matrix_coverage,
    check_pixi_python_ceiling,
    check_project_version_consistency,
    check_python_version_consistency,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
    extract_pixi_python_ceiling,
    extract_pixi_workspace_version,
    extract_project_version,
    extract_pyproject_versions,
    extract_pyproject_versions_str,
    get_dockerfile_python_version,
    main,
)

CONSISTENT_PYPROJECT = """\
[project]
requires-python = ">=3.10"
classifiers = [
    "Programming Language :: Python :: 3.10",
    "Programming Language :: Python :: 3.11",
    "Programming Language :: Python :: 3.12",
]

[tool.mypy]
python_version = "3.10"

[tool.ruff]
target-version = "py310"
"""

INCONSISTENT_PYPROJECT = """\
[project]
requires-python = ">=3.10"

[tool.mypy]
python_version = "3.11"

[tool.ruff]
target-version = "py312"
"""


class TestExtractPyprojectVersions:
    """Tests for extract_pyproject_versions()."""

    def test_extracts_all_versions(self, tmp_path: Path) -> None:
        """Extracts requires-python, classifiers, mypy, and ruff versions."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = extract_pyproject_versions(pyproject)
        assert "requires-python" in versions
        assert versions["requires-python"] == "3.10"
        assert "mypy.python_version" in versions
        assert versions["mypy.python_version"] == "3.10"
        assert "ruff.target-version" in versions
        assert versions["ruff.target-version"] == "3.10"

    def test_extracts_highest_classifier(self, tmp_path: Path) -> None:
        """Picks the highest Python classifier version."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = extract_pyproject_versions(pyproject)
        assert versions.get("classifiers-highest") == "3.12"

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """Non-existent file returns empty dict."""
        versions = extract_pyproject_versions(tmp_path / "nonexistent.toml")
        assert versions == {}

    def test_minimal_pyproject(self, tmp_path: Path) -> None:
        """File with only requires-python still works."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nrequires-python = ">=3.11"\n')
        versions = extract_pyproject_versions(pyproject)
        assert versions.get("requires-python") == "3.11"

    def test_ruff_target_version_not_matched_in_other_sections(self, tmp_path: Path) -> None:
        """target-version in non-[tool.ruff] sections is not extracted."""
        # A different section that happens to have target-version should not match
        content = '[tool.other]\ntarget-version = "py38"\n\n[tool.ruff]\ntarget-version = "py310"\n'
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(content)
        versions = extract_pyproject_versions(pyproject)
        assert versions.get("ruff.target-version") == "3.10"


class TestGetDockerfilePythonVersion:
    """Tests for get_dockerfile_python_version()."""

    def test_extracts_version(self, tmp_path: Path) -> None:
        """Extracts version from FROM python:X.Y line."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM python:3.12-slim\nRUN pip install deps\n")
        assert get_dockerfile_python_version(dockerfile) == "3.12"

    def test_missing_file(self, tmp_path: Path) -> None:
        """Missing Dockerfile returns None."""
        assert get_dockerfile_python_version(tmp_path / "Dockerfile") is None

    def test_no_python_from(self, tmp_path: Path) -> None:
        """Dockerfile without FROM python returns None."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("FROM ubuntu:22.04\n")
        assert get_dockerfile_python_version(dockerfile) is None

    def test_case_insensitive(self, tmp_path: Path) -> None:
        """FROM matching is case-insensitive."""
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text("from python:3.10-slim\n")
        assert get_dockerfile_python_version(dockerfile) == "3.10"


class TestCheckPythonVersionConsistency:
    """Tests for check_python_version_consistency()."""

    def test_consistent_versions(self, tmp_path: Path) -> None:
        """Consistent versions return True."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        consistent, versions = check_python_version_consistency(tmp_path)
        assert consistent is True
        assert len(versions) >= 3

    def test_inconsistent_versions(self, tmp_path: Path) -> None:
        """Inconsistent versions return False."""
        (tmp_path / "pyproject.toml").write_text(INCONSISTENT_PYPROJECT)
        consistent, _versions = check_python_version_consistency(tmp_path)
        assert consistent is False

    def test_single_version_spec(self, tmp_path: Path) -> None:
        """Single version spec is always consistent."""
        (tmp_path / "pyproject.toml").write_text('[project]\nrequires-python = ">=3.10"\n')
        consistent, _versions = check_python_version_consistency(tmp_path)
        assert consistent is True

    def test_with_dockerfile_check(self, tmp_path: Path) -> None:
        """Dockerfile version is included when check_dockerfile=True."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM python:3.10-slim\n")
        consistent, versions = check_python_version_consistency(tmp_path, check_dockerfile=True)
        assert consistent is True
        assert any("Dockerfile" in k for k in versions)

    def test_dockerfile_mismatch(self, tmp_path: Path) -> None:
        """Mismatched Dockerfile version is detected."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM python:3.13-slim\n")
        consistent, _versions = check_python_version_consistency(tmp_path, check_dockerfile=True)
        assert consistent is False

    def test_no_pyproject(self, tmp_path: Path) -> None:
        """Missing pyproject.toml returns consistent (no versions to compare)."""
        consistent, versions = check_python_version_consistency(tmp_path)
        assert consistent is True
        assert versions == {}

    def test_verbose_output(self, tmp_path: Path) -> None:
        """Verbose mode prints version details."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        consistent, _versions = check_python_version_consistency(tmp_path, verbose=True)
        assert consistent is True


class TestExtractViaRegex:
    """Tests for _extract_via_regex fallback."""

    def test_extracts_versions(self, tmp_path: Path) -> None:
        """Regex fallback extracts versions from pyproject.toml."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(CONSISTENT_PYPROJECT)
        versions = _extract_via_regex(pyproject)
        assert "requires-python" in versions
        assert "mypy.python_version" in versions
        assert "ruff.target-version" in versions


class TestMain:
    """Tests for main() CLI entry point."""

    def test_consistent_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Consistent versions exit 0."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 0

    def test_inconsistent_returns_one(self, tmp_path: Path, monkeypatch) -> None:
        """Inconsistent versions exit 1."""
        (tmp_path / "pyproject.toml").write_text(INCONSISTENT_PYPROJECT)
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 1

    def test_no_pyproject_returns_zero(self, tmp_path: Path, monkeypatch) -> None:
        """Missing pyproject.toml exits 0 with warning."""
        monkeypatch.setattr("sys.argv", ["check-python-version", "--repo-root", str(tmp_path)])
        assert main() == 0

    def test_verbose_flag(self, tmp_path: Path, monkeypatch) -> None:
        """Verbose flag is accepted."""
        (tmp_path / "pyproject.toml").write_text(CONSISTENT_PYPROJECT)
        monkeypatch.setattr(
            "sys.argv",
            ["check-python-version", "--repo-root", str(tmp_path), "--verbose"],
        )
        assert main() == 0


# ---------------------------------------------------------------------------
# Ported from tests/unit/scripts_lib/test_check_python_version_consistency.py
# ---------------------------------------------------------------------------


class TestExtractProjectVersion:
    """Tests for extract_project_version()."""

    @pytest.mark.parametrize(
        ("toml_content", "expected"),
        [
            pytest.param(
                '[project]\nname = "foo"\nversion = "1.2.3"\n',
                "1.2.3",
                id="version_present",
            ),
            pytest.param(
                '[project]\nversion = "0.1.0"\n\n[other]\nversion = "9.9.9"\n',
                "0.1.0",
                id="version_first_line",
            ),
            pytest.param(
                '[project]\nname = "foo"\nversion = "2.0.0"\n\n[other]\nversion = "9.9.9"\n',
                "2.0.0",
                id="version_after_other_keys",
            ),
        ],
    )
    def test_extracts_correct_version(self, toml_content: str, expected: str) -> None:
        assert extract_project_version(toml_content) == expected

    @pytest.mark.parametrize(
        ("toml_content",),
        [
            pytest.param(
                '[project]\nname = "foo"\n\n[other]\nversion = "9.9.9"\n',
                id="version_only_in_other_section",
            ),
            pytest.param(
                '[other]\nversion = "1.0.0"\n',
                id="no_project_section",
            ),
            pytest.param(
                "",
                id="empty_content",
            ),
            pytest.param(
                '[project]\nname = "foo"\n',
                id="no_version_key",
            ),
        ],
    )
    def test_returns_none(self, toml_content: str) -> None:
        assert extract_project_version(toml_content) is None


class TestExtractPixiWorkspaceVersion:
    """Tests for extract_pixi_workspace_version()."""

    @pytest.mark.parametrize(
        ("toml_content", "expected"),
        [
            pytest.param(
                '[workspace]\nversion = "1.0.0"\n',
                "1.0.0",
                id="version_present",
            ),
            pytest.param(
                '[workspace]\nname = "bar"\nversion = "3.5.0"\n\n[other]\nversion = "9.9.9"\n',
                "3.5.0",
                id="version_with_other_section",
            ),
        ],
    )
    def test_extracts_correct_version(self, toml_content: str, expected: str) -> None:
        assert extract_pixi_workspace_version(toml_content) == expected

    @pytest.mark.parametrize(
        ("toml_content",),
        [
            pytest.param(
                '[workspace]\nname = "foo"\n\n[other]\nversion = "2.0.0"\n',
                id="version_only_in_other_section",
            ),
            pytest.param(
                "",
                id="empty_content",
            ),
            pytest.param(
                '[other]\nversion = "1.0.0"\n',
                id="no_workspace_section",
            ),
        ],
    )
    def test_returns_none(self, toml_content: str) -> None:
        assert extract_pixi_workspace_version(toml_content) is None


class TestExtractPyprojectVersionsStr:
    """Tests for extract_pyproject_versions_str() — verifies section-bounded mypy regex."""

    def test_mypy_version_extracted(self) -> None:
        content = '[tool.mypy]\npython_version = "3.10"\n'
        versions = extract_pyproject_versions_str(content)
        assert versions["mypy.python_version"] == "3.10"

    def test_mypy_version_with_other_keys(self) -> None:
        content = (
            "[tool.mypy]\n"
            "strict = true\n"
            'python_version = "3.11"\n'
            "\n"
            "[tool.ruff]\n"
            'target-version = "py311"\n'
        )
        versions = extract_pyproject_versions_str(content)
        assert versions["mypy.python_version"] == "3.11"
        assert versions["ruff.target-version"] == "3.11"

    def test_mypy_version_not_crossed_from_other_section(self) -> None:
        """Section-bounded regex must NOT cross [tool.other] to find python_version."""
        content = '[tool.mypy]\nstrict = true\n\n[tool.other]\npython_version = "3.12"\n'
        versions = extract_pyproject_versions_str(content)
        assert "mypy.python_version" not in versions

    def test_requires_python_extracted(self) -> None:
        content = 'requires-python = ">=3.10"\n'
        versions = extract_pyproject_versions_str(content)
        assert versions["requires-python"] == "3.10"

    def test_empty_content(self) -> None:
        assert extract_pyproject_versions_str("") == {}


class TestExtractClassifiersPythonVersions:
    """Tests for extract_classifiers_python_versions()."""

    def test_extracts_multiple_versions(self) -> None:
        content = (
            "classifiers = [\n"
            '    "Programming Language :: Python :: 3.10",\n'
            '    "Programming Language :: Python :: 3.11",\n'
            '    "Programming Language :: Python :: 3.12",\n'
            "]\n"
        )
        assert extract_classifiers_python_versions(content) == ["3.10", "3.11", "3.12"]

    def test_returns_empty_when_no_classifiers(self) -> None:
        assert extract_classifiers_python_versions("[project]\nname = 'mypkg'\n") == []

    def test_deduplicates_versions(self) -> None:
        content = (
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.10",\n'
        )
        assert extract_classifiers_python_versions(content) == ["3.10"]

    def test_returns_sorted_versions(self) -> None:
        content = (
            '"Programming Language :: Python :: 3.12",\n'
            '"Programming Language :: Python :: 3.10",\n'
            '"Programming Language :: Python :: 3.11",\n'
        )
        assert extract_classifiers_python_versions(content) == ["3.10", "3.11", "3.12"]


class TestExtractCiMatrixPythonVersions:
    """Tests for extract_ci_matrix_python_versions()."""

    def test_extracts_quoted_versions(self) -> None:
        assert extract_ci_matrix_python_versions('python-version: ["3.10", "3.11", "3.12"]\n') == [
            "3.10",
            "3.11",
            "3.12",
        ]

    def test_extracts_unquoted_versions(self) -> None:
        assert extract_ci_matrix_python_versions("python-version: [3.10, 3.11]\n") == [
            "3.10",
            "3.11",
        ]

    def test_returns_empty_when_no_matrix(self) -> None:
        content = "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
        assert extract_ci_matrix_python_versions(content) == []

    def test_deduplicates_and_sorts(self) -> None:
        assert extract_ci_matrix_python_versions('python-version: ["3.12", "3.10", "3.12"]\n') == [
            "3.10",
            "3.12",
        ]


class TestExtractPixiPythonCeiling:
    """Tests for extract_pixi_python_ceiling()."""

    @pytest.mark.parametrize(
        ("toml_content", "expected"),
        [
            pytest.param(
                '[dependencies]\npython = ">=3.10,<3.14"\npip = "*"\n',
                "3.14",
                id="bounded_exclusive",
            ),
            pytest.param(
                '[dependencies]\npython = ">=3.10,<=3.13"\n',
                "3.13",
                id="bounded_inclusive",
            ),
            pytest.param(
                '[dependencies]\nname = "x"\npython = ">=3.11,<3.15"\n',
                "3.15",
                id="bound_after_other_key",
            ),
        ],
    )
    def test_extracts_ceiling(self, toml_content: str, expected: str) -> None:
        assert extract_pixi_python_ceiling(toml_content) == expected

    @pytest.mark.parametrize(
        ("toml_content",),
        [
            pytest.param('[dependencies]\npython = ">=3.10"\n', id="unbounded"),
            pytest.param('[dependencies]\npip = "*"\n', id="no_python_key"),
            pytest.param("", id="empty_content"),
            pytest.param(
                '[feature.lint.dependencies]\npython = ">=3.10,<3.14"\n',
                id="python_only_in_other_section",
            ),
        ],
    )
    def test_returns_none(self, toml_content: str) -> None:
        assert extract_pixi_python_ceiling(toml_content) is None


class TestCheckCiMatrixCoverage:
    """Tests for check_ci_matrix_coverage()."""

    def test_returns_true_when_matrix_covers_all(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.11",\n'
        )
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10", "3.11"]\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_false_when_matrix_missing_version(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.12",\n'
        )
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10"]\n')
        assert check_ci_matrix_coverage(tmp_path) is False

    def test_returns_true_when_no_pyproject(self, tmp_path: Path) -> None:
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_true_when_no_classifiers(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "mypkg"\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_true_when_no_ci_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('"Programming Language :: Python :: 3.10",\n')
        assert check_ci_matrix_coverage(tmp_path) is True
        assert "INFO:" in capsys.readouterr().out

    def test_returns_true_when_no_matrix_in_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "pyproject.toml").write_text('"Programming Language :: Python :: 3.10",\n')
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text("jobs:\n  test:\n    runs-on: ubuntu-latest\n")
        assert check_ci_matrix_coverage(tmp_path) is True
        assert "INFO:" in capsys.readouterr().out

    def test_returns_true_when_matrix_has_extra_versions(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('"Programming Language :: Python :: 3.10",\n')
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10", "3.11", "3.12"]\n')
        assert check_ci_matrix_coverage(tmp_path) is True


class TestCheckPixiPythonCeiling:
    """Tests for check_pixi_python_ceiling()."""

    _CLASSIFIERS_313 = (
        "classifiers = [\n"
        '    "Programming Language :: Python :: 3.10",\n'
        '    "Programming Language :: Python :: 3.13",\n'
        "]\n"
    )

    def _write(self, tmp_path: Path, pyproject: str, pixi: str) -> None:
        (tmp_path / "pyproject.toml").write_text(pyproject)
        (tmp_path / "pixi.toml").write_text(pixi)

    def test_rejects_unbounded(self, tmp_path: Path) -> None:
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_python_ceiling(tmp_path) is False

    def test_accepts_next_minor(self, tmp_path: Path) -> None:
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.14"\n')
        assert check_pixi_python_ceiling(tmp_path) is True

    def test_rejects_too_high(self, tmp_path: Path) -> None:
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.16"\n')
        assert check_pixi_python_ceiling(tmp_path) is False

    def test_normalizes_with_version_not_string(self, tmp_path: Path) -> None:
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.14"\n')
        assert check_pixi_python_ceiling(tmp_path) is True

    def test_returns_true_when_no_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pixi.toml").write_text('[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_python_ceiling(tmp_path) is True

    def test_returns_true_when_no_pixi(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(self._CLASSIFIERS_313)
        assert check_pixi_python_ceiling(tmp_path) is True

    def test_returns_true_when_no_classifiers(self, tmp_path: Path) -> None:
        self._write(tmp_path, '[project]\nname = "x"\n', '[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_python_ceiling(tmp_path) is True


class TestCheckProjectVersionConsistency:
    """Tests for check_project_version_consistency()."""

    def test_returns_true_when_pixi_has_no_version(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "foo"\nversion = "1.0.0"\n')
        (tmp_path / "pixi.toml").write_text('[workspace]\nname = "foo"\n')
        assert check_project_version_consistency(tmp_path) is True

    def test_returns_false_when_versions_mismatch(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        (tmp_path / "pixi.toml").write_text('[workspace]\nversion = "2.0.0"\n')
        assert check_project_version_consistency(tmp_path) is False

    def test_returns_true_when_versions_match(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        (tmp_path / "pixi.toml").write_text('[workspace]\nversion = "1.0.0"\n')
        assert check_project_version_consistency(tmp_path) is True

    def test_returns_true_when_no_pyproject(self, tmp_path: Path) -> None:
        assert check_project_version_consistency(tmp_path) is True

    def test_returns_true_when_no_pixi(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.0.0"\n')
        assert check_project_version_consistency(tmp_path) is True


class TestSmokeAgainstRealFiles:
    """Verify extraction against actual config files in the repo."""

    @pytest.fixture()
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    def test_pyproject_toml_str_api(self, repo_root: Path) -> None:
        path = repo_root / "pyproject.toml"
        if not path.exists():
            pytest.skip("pyproject.toml not found")
        versions = extract_pyproject_versions_str(path.read_text())
        assert "requires-python" in versions

    def test_pixi_toml_no_workspace_version(self, repo_root: Path) -> None:
        path = repo_root / "pixi.toml"
        if not path.exists():
            pytest.skip("pixi.toml not found")
        version = extract_pixi_workspace_version(path.read_text())
        assert version is None, (
            "pixi.toml [workspace] must not contain a version field — "
            "pyproject.toml is the single source of truth."
        )
