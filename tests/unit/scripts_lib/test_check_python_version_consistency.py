"""Tests for scripts/check_python_version_consistency.py.

Validates that version extraction regexes are bounded to their TOML sections
and do not match keys from unrelated sections.
"""

from pathlib import Path

import pytest

from hephaestus.scripts_lib.check_python_version_consistency import (
    check_ci_matrix_coverage,
    check_pixi_python_ceiling,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
    extract_pixi_python_ceiling,
    extract_pixi_workspace_version,
    extract_project_version,
    extract_pyproject_versions,
)


# ---------------------------------------------------------------------------
# extract_project_version
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


# ---------------------------------------------------------------------------
# extract_pixi_workspace_version
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# extract_pyproject_versions — mypy regex boundary fix
# ---------------------------------------------------------------------------
class TestExtractPyprojectVersions:
    """Tests for extract_pyproject_versions(), focusing on the mypy regex fix."""

    def test_mypy_version_extracted(self) -> None:
        content = '[tool.mypy]\npython_version = "3.10"\n'
        versions = extract_pyproject_versions(content)
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
        versions = extract_pyproject_versions(content)
        assert versions["mypy.python_version"] == "3.11"
        assert versions["ruff.target-version"] == "3.11"

    def test_mypy_version_not_crossed_from_other_section(self) -> None:
        """The regex must NOT cross into [other] to find python_version."""
        content = '[tool.mypy]\nstrict = true\n\n[tool.other]\npython_version = "3.12"\n'
        versions = extract_pyproject_versions(content)
        assert "mypy.python_version" not in versions

    def test_requires_python_extracted(self) -> None:
        content = 'requires-python = ">=3.10"\n'
        versions = extract_pyproject_versions(content)
        assert versions["requires-python"] == "3.10"

    def test_empty_content(self) -> None:
        assert extract_pyproject_versions("") == {}


# ---------------------------------------------------------------------------
# extract_classifiers_python_versions
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# extract_ci_matrix_python_versions
# ---------------------------------------------------------------------------
class TestExtractCiMatrixPythonVersions:
    """Tests for extract_ci_matrix_python_versions()."""

    def test_extracts_quoted_versions(self) -> None:
        content = 'python-version: ["3.10", "3.11", "3.12"]\n'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11", "3.12"]

    def test_extracts_unquoted_versions(self) -> None:
        content = "python-version: [3.10, 3.11]\n"
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11"]

    def test_returns_empty_when_no_matrix(self) -> None:
        content = "jobs:\n  test:\n    runs-on: ubuntu-latest\n"
        assert extract_ci_matrix_python_versions(content) == []

    def test_deduplicates_and_sorts(self) -> None:
        content = 'python-version: ["3.12", "3.10", "3.12"]\n'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.12"]

    def test_extracts_sequence_format_double_quoted(self) -> None:
        content = 'python-version:\n  - "3.10"\n  - "3.11"\n  - "3.12"\n'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11", "3.12"]

    def test_extracts_sequence_format_unquoted(self) -> None:
        content = "python-version:\n  - 3.10\n  - 3.11\n"
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11"]

    def test_extracts_sequence_format_single_quoted(self) -> None:
        content = "python-version:\n  - '3.10'\n  - '3.12'\n"
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.12"]

    def test_extracts_sequence_format_deduplicates_and_sorts(self) -> None:
        content = 'python-version:\n  - "3.12"\n  - "3.10"\n  - "3.12"\n'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.12"]

    def test_bracket_format_takes_precedence_when_both_present(self) -> None:
        content = 'python-version: ["3.11"]\npython-version:\n  - "3.10"\n'
        assert extract_ci_matrix_python_versions(content) == ["3.11"]

    def test_sequence_format_with_4_space_indent(self) -> None:
        content = 'python-version:\n    - "3.10"\n    - "3.11"\n'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11"]

    def test_sequence_format_no_trailing_newline(self) -> None:
        content = 'python-version:\n  - "3.10"\n  - "3.11"'
        assert extract_ci_matrix_python_versions(content) == ["3.10", "3.11"]


# ---------------------------------------------------------------------------
# check_ci_matrix_coverage
# ---------------------------------------------------------------------------
class TestCheckCiMatrixCoverage:
    """Tests for check_ci_matrix_coverage()."""

    def test_returns_true_when_matrix_covers_all(self, tmp_path: Path) -> None:
        """Returns True when CI matrix includes all classifier versions."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.11",\n'
        )
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10", "3.11"]\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_false_when_matrix_missing_version(self, tmp_path: Path) -> None:
        """Returns False when CI matrix is missing a classifier version."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.12",\n'
        )
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10"]\n')
        assert check_ci_matrix_coverage(tmp_path) is False

    def test_returns_true_when_no_pyproject(self, tmp_path: Path) -> None:
        """Returns True when pyproject.toml does not exist."""
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_true_when_no_classifiers(self, tmp_path: Path) -> None:
        """Returns True when pyproject.toml has no Python classifiers."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "mypkg"\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_true_when_no_ci_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Returns True with INFO (not WARNING) when CI workflow file does not exist."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('"Programming Language :: Python :: 3.10",\n')
        assert check_ci_matrix_coverage(tmp_path) is True
        captured = capsys.readouterr().out
        assert "INFO:" in captured
        assert "WARNING:" not in captured

    def test_returns_true_when_no_matrix_in_workflow(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Returns True with INFO (not WARNING) when workflow has no python-version matrix."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('"Programming Language :: Python :: 3.10",\n')
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text("jobs:\n  test:\n    runs-on: ubuntu-latest\n")
        assert check_ci_matrix_coverage(tmp_path) is True
        captured = capsys.readouterr().out
        assert "INFO:" in captured
        assert "WARNING:" not in captured

    def test_returns_true_when_matrix_has_extra_versions(self, tmp_path: Path) -> None:
        """Returns True when CI matrix has versions beyond what classifiers list."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('"Programming Language :: Python :: 3.10",\n')
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10", "3.11", "3.12"]\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_sequence_format_workflow_is_parsed(self, tmp_path: Path) -> None:
        """check_ci_matrix_coverage reads sequence-format python-version correctly."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '"Programming Language :: Python :: 3.10",\n"Programming Language :: Python :: 3.11",\n'
        )
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text("python-version:\n  - '3.10'\n  - '3.11'\n")
        assert check_ci_matrix_coverage(tmp_path) is True


# ---------------------------------------------------------------------------
# extract_pixi_python_ceiling
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# check_pixi_python_ceiling
# ---------------------------------------------------------------------------
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
        """An unbounded python pin fails — env may resolve to an untested interpreter."""
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10"\n')
        assert check_pixi_python_ceiling(tmp_path) is False

    def test_accepts_next_minor(self, tmp_path: Path) -> None:
        """A cap one minor above the highest classifier (<3.14 for 3.13) is accepted."""
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.14"\n')
        assert check_pixi_python_ceiling(tmp_path) is True

    def test_rejects_too_high(self, tmp_path: Path) -> None:
        """A cap more than one minor above the highest classifier fails."""
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.16"\n')
        assert check_pixi_python_ceiling(tmp_path) is False

    def test_normalizes_with_version_not_string(self, tmp_path: Path) -> None:
        """The cap is compared via packaging.Version, not string equality.

        ``<3.14`` exactly equals the allowed cap ``3.14`` for classifier 3.13;
        a naive string compare of ``"3.14"`` vs a computed ``"3.14"`` would
        accidentally pass, but the boundary (equal, not greater) must hold.
        """
        self._write(tmp_path, self._CLASSIFIERS_313, '[dependencies]\npython = ">=3.10,<3.14"\n')
        # Equal-to-max-allowed is accepted (not a strict-greater rejection).
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


# ---------------------------------------------------------------------------
# Smoke tests against real repo files
# ---------------------------------------------------------------------------
class TestSmokeAgainstRealFiles:
    """Verify extraction against actual config files in the repo."""

    @pytest.fixture()
    def repo_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    def test_pyproject_toml(self, repo_root: Path) -> None:
        path = repo_root / "pyproject.toml"
        if not path.exists():
            pytest.skip("pyproject.toml not found")
        content = path.read_text()
        versions = extract_pyproject_versions(content)
        # Should find at least requires-python
        assert "requires-python" in versions

    def test_pixi_toml(self, repo_root: Path) -> None:
        path = repo_root / "pixi.toml"
        if not path.exists():
            pytest.skip("pixi.toml not found")
        content = path.read_text()
        version = extract_pixi_workspace_version(content)
        # pyproject.toml is the single source of truth for the project version.
        # pixi.toml [workspace] must NOT declare a version field.
        assert version is None, (
            "pixi.toml [workspace] must not contain a version field — "
            "pyproject.toml is the single source of truth for the project version."
        )
