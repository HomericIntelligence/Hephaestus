"""Tests for scripts/check_python_version_consistency.py.

Validates that version extraction regexes are bounded to their TOML sections
and do not match keys from unrelated sections.
"""

import sys
from pathlib import Path

import pytest

# Add the scripts directory to the path so we can import the module directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from check_python_version_consistency import (
    check_ci_matrix_coverage,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
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

    def test_returns_true_when_no_ci_workflow(self, tmp_path: Path) -> None:
        """Returns True (with warning) when CI workflow file does not exist."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('"Programming Language :: Python :: 3.10",\n')
        assert check_ci_matrix_coverage(tmp_path) is True

    def test_returns_true_when_matrix_has_extra_versions(self, tmp_path: Path) -> None:
        """Returns True when CI matrix has versions beyond what classifiers list."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('"Programming Language :: Python :: 3.10",\n')
        workflow_dir = tmp_path / ".github" / "workflows"
        workflow_dir.mkdir(parents=True)
        (workflow_dir / "test.yml").write_text('python-version: ["3.10", "3.11", "3.12"]\n')
        assert check_ci_matrix_coverage(tmp_path) is True


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
