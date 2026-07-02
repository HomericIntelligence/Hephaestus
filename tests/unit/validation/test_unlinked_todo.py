"""Tests for TODO/FIXME/HACK issue-link enforcement."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.utils.helpers import get_repo_root
from hephaestus.validation.unlinked_todo import (
    find_unlinked_todos,
    main,
    scan_file,
)


def test_scan_file_accepts_issue_linked_markers(tmp_path: Path) -> None:
    """Markers in the documented ``# TODO(#N):`` form are accepted."""
    source = tmp_path / "ok.py"
    source.write_text(
        "# TODO(#1738): keep the pre-commit hook wired\n"
        "value = 1  # FIXME(#42): inline comments use the same contract\n"
        "# HACK(#7): explain why the workaround exists\n",
        encoding="utf-8",
    )

    assert scan_file(source, tmp_path) == []


def test_scan_file_reports_bare_marker(tmp_path: Path) -> None:
    """A bare marker without an issue reference is a violation."""
    source = tmp_path / "bad.py"
    source.write_text("# TODO: wire this later\n", encoding="utf-8")

    findings = scan_file(source, tmp_path)

    assert len(findings) == 1
    assert findings[0].path == "bad.py"
    assert findings[0].line == 1
    assert findings[0].marker == "TODO"


def test_marker_requires_documented_issue_form(tmp_path: Path) -> None:
    """A loose issue mention is not a substitute for ``# TODO(#N):``."""
    source = tmp_path / "bad.py"
    source.write_text("# TODO see #1738: not the documented form\n", encoding="utf-8")

    findings = scan_file(source, tmp_path)

    assert len(findings) == 1
    assert findings[0].marker == "TODO"


def test_scan_file_ignores_strings_and_docstrings(tmp_path: Path) -> None:
    """Only Python comment tokens are scanned for debt markers."""
    source = tmp_path / "strings.py"
    source.write_text(
        '"""TODO: this appears in a docstring, not a comment."""\n'
        'text = "# FIXME: this appears in a string"\n',
        encoding="utf-8",
    )

    assert scan_file(source, tmp_path) == []


def test_find_unlinked_todos_recurses_python_files_only(tmp_path: Path) -> None:
    """Directory scans recurse through Python files and ignore other suffixes."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "bad.py").write_text("# HACK: missing issue\n", encoding="utf-8")
    (package / "notes.txt").write_text("# TODO: prose is not Python source\n", encoding="utf-8")

    findings = find_unlinked_todos(tmp_path, paths=[Path("hephaestus")])

    assert [finding.path for finding in findings] == ["hephaestus/bad.py"]


def test_main_reports_json_violations(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI returns non-zero and emits structured findings in JSON mode."""
    package = tmp_path / "hephaestus"
    package.mkdir()
    (package / "bad.py").write_text("# FIXME: missing issue\n", encoding="utf-8")

    assert main(["--repo-root", str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "violations": [
            {
                "path": "hephaestus/bad.py",
                "line": 1,
                "marker": "FIXME",
                "text": "# FIXME: missing issue",
            }
        ]
    }


def test_main_passes_on_real_repository() -> None:
    """The shipped tree must satisfy the TODO issue-link invariant."""
    assert main(["--repo-root", str(get_repo_root())]) == 0
