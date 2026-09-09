"""Tests for Python-version consistency validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.validation import python_version as pv
from hephaestus.validation.python_version import (
    check_ci_matrix_coverage,
    check_python_version_consistency,
    extract_ci_matrix_python_versions,
    extract_classifiers_python_versions,
    extract_pyproject_versions,
    extract_pyproject_versions_str,
    get_dockerfile_python_version,
)

PYPROJECT = """[project]
requires-python = ">=3.13,<3.14"
classifiers = ["Programming Language :: Python :: 3.13"]
[tool.mypy]
python_version = "3.13"
[tool.ruff]
target-version = "py313"
"""


def test_project_version_declarations_are_compared(tmp_path: Path) -> None:
    """Matching project tool declarations form one supported base version."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    consistent, versions = check_python_version_consistency(tmp_path)
    assert consistent is True
    assert versions["requires-python"] == "3.13"


def test_project_version_mismatch_is_detected(tmp_path: Path) -> None:
    """A tool targeting a different base Python fails consistency validation."""
    mismatched = PYPROJECT.replace('python_version = "3.13"', 'python_version = "3.12"')
    (tmp_path / "pyproject.toml").write_text(mismatched)
    consistent, _ = check_python_version_consistency(tmp_path)
    assert consistent is False


def test_ci_matrix_parser_returns_all_configured_versions() -> None:
    """The CI guard reads every explicitly configured Python version."""
    assert extract_ci_matrix_python_versions('python-version: ["3.13"]') == ["3.13"]


def test_ci_matrix_must_cover_declared_classifiers(tmp_path: Path) -> None:
    """A classifier missing from CI makes the repository contract fail."""
    missing_classifier = PYPROJECT.replace(
        '3.13"]', '3.13", "Programming Language :: Python :: 3.12"]'
    )
    (tmp_path / "pyproject.toml").write_text(missing_classifier)
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "test.yml").write_text('python-version: ["3.13"]')
    assert check_ci_matrix_coverage(tmp_path) is False


def test_pyproject_reader_handles_missing_file(tmp_path: Path) -> None:
    """A missing metadata file produces no declarations rather than an error."""
    assert extract_pyproject_versions(tmp_path / "pyproject.toml") == {}


def test_pyproject_reader_ignores_unrecognized_declarations(tmp_path: Path) -> None:
    """Unrecognized metadata values do not become false version declarations."""
    (tmp_path / "pyproject.toml").write_text(
        """[project]
requires-python = "stable"
classifiers = ["Programming Language :: Python"]
[tool.mypy]
python_version = ""
[tool.ruff]
target-version = "latest"
""",
        encoding="utf-8",
    )
    assert extract_pyproject_versions(tmp_path / "pyproject.toml") == {}


def test_pyproject_reader_selects_highest_supported_classifier(tmp_path: Path) -> None:
    """Classifier extraction selects the highest declared Python version."""
    (tmp_path / "pyproject.toml").write_text(
        PYPROJECT.replace(
            'classifiers = ["Programming Language :: Python :: 3.13"]',
            'classifiers = ["Programming Language :: Python :: 3.12", '
            '"Programming Language :: Python :: 3.13", "Other"]',
        ),
        encoding="utf-8",
    )
    assert extract_pyproject_versions(tmp_path / "pyproject.toml")["classifiers-highest"] == (
        "3.13"
    )


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            PYPROJECT,
            {
                "requires-python": "3.13",
                "mypy.python_version": "3.13",
                "ruff.target-version": "3.13",
            },
        ),
        ("[project]\nname='x'\n", {}),
        ('[tool.ruff.lint]\nselect=["E"]\n', {}),
    ],
)
def test_raw_pyproject_parser_extracts_only_base_tool_sections(
    content: str, expected: dict[str, str]
) -> None:
    """Raw metadata parsing keeps only declarations from their owning sections."""
    assert extract_pyproject_versions_str(content) == expected


def test_dockerfile_version_reader_handles_present_absent_and_missing(tmp_path: Path) -> None:
    """Dockerfile parsing returns a base version only when a Python image exists."""
    dockerfile = tmp_path / "Dockerfile"
    assert get_dockerfile_python_version(dockerfile) is None
    dockerfile.write_text("FROM ubuntu:24.04\n", encoding="utf-8")
    assert get_dockerfile_python_version(dockerfile) is None
    dockerfile.write_text("  from Python:3.13-slim\n", encoding="utf-8")
    assert get_dockerfile_python_version(dockerfile) == "3.13"


def test_classifier_parser_deduplicates_and_sorts_versions() -> None:
    """Classifier parsing returns one sorted value for each advertised version."""
    content = (
        '"Programming Language :: Python :: 3.13"\n'
        '"Programming Language :: Python :: 3.12"\n'
        '"Programming Language :: Python :: 3.13"\n'
    )
    assert extract_classifiers_python_versions(content) == ["3.12", "3.13"]


def test_ci_matrix_parser_handles_absent_and_duplicate_values() -> None:
    """CI matrix parsing returns no values when absent and deduplicates when present."""
    assert extract_ci_matrix_python_versions("jobs: {}") == []
    assert extract_ci_matrix_python_versions("python-version: ['3.13', 3.12, '3.13']") == [
        "3.12",
        "3.13",
    ]


def test_ci_matrix_check_accepts_missing_inputs_and_complete_matrix(tmp_path: Path) -> None:
    """The matrix check skips absent contracts and accepts complete coverage."""
    assert check_ci_matrix_coverage(tmp_path) is True
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    workflow = tmp_path / ".github" / "workflows"
    workflow.mkdir(parents=True)
    (workflow / "test.yml").write_text('python-version: ["3.13"]', encoding="utf-8")
    assert check_ci_matrix_coverage(tmp_path) is True


def test_consistency_can_include_nested_dockerfile_and_verbose_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Docker validation selects the first available image and reports all declarations."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    docker_dir = tmp_path / "docker"
    docker_dir.mkdir()
    (docker_dir / "Dockerfile").write_text("FROM python:3.12\n", encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13\n", encoding="utf-8")

    consistent, versions = check_python_version_consistency(
        tmp_path, check_dockerfile=True, verbose=True
    )

    assert consistent is False
    assert versions["Dockerfile (docker/Dockerfile)"] == "3.12"
    assert "Dockerfile (docker/Dockerfile): 3.12" in capsys.readouterr().out


def test_consistency_uses_top_level_dockerfile_fallback(tmp_path: Path) -> None:
    """Docker validation uses the top-level file when the nested file has no Python base."""
    (tmp_path / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13\n", encoding="utf-8")
    consistent, versions = check_python_version_consistency(tmp_path, check_dockerfile=True)
    assert consistent is True
    assert versions["Dockerfile (Dockerfile)"] == "3.13"


@pytest.mark.parametrize(
    ("argv", "consistent", "versions", "matrix_ok", "expected_rc", "stream_text"),
    [
        (["check"], True, {"requires-python": "3.13"}, True, 0, "specifications are consistent"),
        (
            ["check"],
            False,
            {"requires-python": "3.13", "mypy.python_version": "3.12"},
            True,
            1,
            "inconsistency detected",
        ),
        (["check"], False, {}, True, 0, "inconsistency detected"),
        (["check"], True, {"requires-python": "3.13"}, False, 1, "specifications are consistent"),
    ],
)
def test_main_combines_consistency_and_matrix_results(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    argv: list[str],
    consistent: bool,
    versions: dict[str, str],
    matrix_ok: bool,
    expected_rc: int,
    stream_text: str,
) -> None:
    """The CLI combines declaration and matrix results into one exit status."""
    monkeypatch.setattr("hephaestus.validation.python_version.sys.argv", argv)
    monkeypatch.setattr(pv, "resolve_repo_root", lambda args: tmp_path)
    monkeypatch.setattr(
        pv,
        "check_python_version_consistency",
        lambda *args, **kwargs: (consistent, versions),
    )
    monkeypatch.setattr(pv, "check_ci_matrix_coverage", lambda root: matrix_ok)

    assert pv.main() == expected_rc
    captured = capsys.readouterr()
    assert stream_text in captured.out + captured.err


def test_main_emits_json_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """JSON mode emits the complete machine-readable consistency result."""
    monkeypatch.setattr("hephaestus.validation.python_version.sys.argv", ["check", "--json"])
    monkeypatch.setattr(pv, "resolve_repo_root", lambda args: tmp_path)
    monkeypatch.setattr(
        pv,
        "check_python_version_consistency",
        lambda *args, **kwargs: (True, {"requires-python": "3.13"}),
    )
    monkeypatch.setattr(pv, "check_ci_matrix_coverage", lambda root: True)

    assert pv.main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "consistent": True,
        "passed": True,
        "versions": {"requires-python": "3.13"},
    }
