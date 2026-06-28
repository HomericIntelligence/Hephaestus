"""CLI contract tests for shared validation parser migrations."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from hephaestus.validation import (
    cli_tier_docs,
    doc_config,
    docstrings,
    markdown,
    mypy_per_file,
    skill_catalog,
)


def test_docstrings_main_accepts_repo_root_and_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """docstrings.main() keeps explicit --repo-root and --json behavior."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hephaestus-check-docstrings",
            "--repo-root",
            str(tmp_path),
            "--directory",
            str(tmp_path),
            "--json",
        ],
    )
    monkeypatch.setattr(docstrings, "scan_directory", lambda directory, repo_root: [])

    assert docstrings.main() == 0
    assert json.loads(capsys.readouterr().out) == []


def test_doc_config_main_uses_shared_repo_root_json_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """doc_config.main() reads repo_root from the shared parser in JSON mode."""
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hephaestus-check-doc-config",
            "--repo-root",
            str(tmp_path),
            "--skip-test-count",
            "--json",
        ],
    )
    monkeypatch.setattr(doc_config, "load_coverage_threshold", lambda repo_root: 83.0)
    monkeypatch.setattr(doc_config, "extract_cov_path", lambda repo_root: "hephaestus")
    monkeypatch.setattr(doc_config, "check_claude_md_threshold", lambda *args: [])
    monkeypatch.setattr(doc_config, "check_readme_cov_path", lambda *args: [])
    monkeypatch.setattr(doc_config, "check_addopts_cov_fail_under", lambda *args: [])

    assert doc_config.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["expected_threshold"] == 83.0
    assert payload["passed"] is True


def test_mypy_per_file_preserves_unknown_flag_passthrough(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """mypy_per_file.main() parses --json without swallowing mypy flags."""
    observed: dict[str, Any] = {}

    def fake_run(files: list[str], flags: list[str] | None = None) -> int:
        observed["files"] = files
        observed["flags"] = flags
        return 0

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hephaestus-mypy-each-file",
            "--json",
            "--strict",
            "--python-version",
            "3.13",
            "module.py",
        ],
    )
    monkeypatch.setattr(mypy_per_file, "run_mypy_per_file", fake_run)

    assert mypy_per_file.main() == 0
    assert observed == {
        "files": ["module.py"],
        "flags": ["--strict", "--python-version", "3.13"],
    }
    assert json.loads(capsys.readouterr().out)["files_checked"] == 1


def test_skill_catalog_defaults_derive_from_repo_root_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """skill_catalog.main() still derives default paths from --repo-root."""
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "plugin-installation.md").write_text(
        "# Plugin\n\n"
        "| Skill | Invocation | Description |\n"
        "|-------|------------|-------------|\n"
        "| alpha | `/alpha` | Test skill |\n",
        encoding="utf-8",
    )
    skill_dir = tmp_path / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Test skill\n---\n\n# Alpha\n",
        encoding="utf-8",
    )

    assert skill_catalog.main(["--repo-root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["missing"] == []
    assert payload["extra"] == []


def test_cli_tier_docs_main_accepts_argv_repo_root_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """cli_tier_docs.main(argv) keeps explicit repo-root and JSON behavior."""
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\n\n[project.scripts]\nhephaestus-demo = "pkg:main"\n',
        encoding="utf-8",
    )
    (tmp_path / "COMPATIBILITY.md").write_text(
        "## Console-Script Stability Tiers\n"
        "| CLI | Tier |\n"
        "|-----|------|\n"
        "| `hephaestus-demo` | Stable |\n",
        encoding="utf-8",
    )

    assert cli_tier_docs.main(["--repo-root", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"violations": []}


def test_markdown_readme_entry_point_does_not_require_repo_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """check_readmes_main() keeps its no-repo-root CLI contract."""
    monkeypatch.setattr(
        sys,
        "argv",
        ["hephaestus-check-readmes", "--directory", str(tmp_path), "--json"],
    )

    assert markdown.check_readmes_main() == 0
    assert json.loads(capsys.readouterr().out) == {"directory": str(tmp_path), "results": []}


def test_markdown_link_entry_point_accepts_repo_root_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """markdown.main() keeps explicit --repo-root and --json behavior."""
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "hephaestus-validate-links",
            str(tmp_path),
            "--repo-root",
            str(tmp_path),
            "--json",
        ],
    )

    assert markdown.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["failed"] == []
