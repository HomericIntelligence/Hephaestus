"""Tests for the README console-script documentation check."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hephaestus.scripts_lib import check_cli_table_sync as mod

REPO_ROOT = Path(__file__).resolve().parents[3]


def test_load_scripts_uses_shared_toml_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Script discovery delegates TOML parsing to the shared resolver."""
    (tmp_path / "pyproject.toml").write_text("not parsed directly", encoding="utf-8")
    parser = SimpleNamespace(
        loads=lambda _source: {"project": {"scripts": {"hephaestus-example": "example:main"}}}
    )
    monkeypatch.setattr(mod, "import_tomllib", lambda: parser)

    assert mod._load_scripts(tmp_path) == {"hephaestus-example"}


def test_readme_command_extraction_uses_inline_command_references(tmp_path: Path) -> None:
    """Only backticked ``hephaestus-*`` commands are documentation references."""
    (tmp_path / "README.md").write_text(
        "Run `hephaestus-automation-loop` and `hephaestus-review-prs`.\n"
        "Do not treat hephaestus-unquoted as a command reference.\n",
        encoding="utf-8",
    )

    assert mod._readme_documented_commands(tmp_path) == {
        "hephaestus-automation-loop",
        "hephaestus-review-prs",
    }


def test_main_reports_declared_command_missing_from_readme(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A packaged command must have a README reference for user discovery."""
    monkeypatch.setattr(mod, "_load_scripts", lambda: {"hephaestus-missing"})
    monkeypatch.setattr(mod, "_readme_documented_commands", set)

    assert mod.main() == 1
    assert "hephaestus-missing" in capsys.readouterr().out


def test_main_passes_against_real_repository(capsys: pytest.CaptureFixture[str]) -> None:
    """The repository documents every currently packaged console script."""
    assert mod.main() == 0
    assert "OK" in capsys.readouterr().out


def test_compatibility_prose_count_drift_is_reported(tmp_path: Path) -> None:
    """The documented compatibility count must derive from ``[project.scripts]``."""
    (tmp_path / "README.md").write_text("49 console scripts are installed.\n", encoding="utf-8")
    (tmp_path / "COMPATIBILITY.md").write_text(
        "Hephaestus installs 48 console scripts via `[project.scripts]`.\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "index.md").write_text(
        "Full signatures for all 49 CLI entry points.\n", encoding="utf-8"
    )

    ok, mismatches = mod.check_prose_counts(tmp_path, expected_count=49)

    assert not ok
    assert any("COMPATIBILITY.md" in mismatch and "48" in mismatch for mismatch in mismatches)


def test_docs_reference_to_unknown_command_is_reported(tmp_path: Path) -> None:
    """A documented CLI command must be registered in ``[project.scripts]``."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text("Run `hephaestus-ghost-command`.\n", encoding="utf-8")

    problems = mod.check_docs_command_references(tmp_path, {"hephaestus-gh"})

    assert problems == [
        "docs/runbook.md: references `hephaestus-ghost-command` "
        "which is not in pyproject.toml [project.scripts]"
    ]


@pytest.mark.parametrize(
    "current_document",
    [
        "README.md",
        "docs/adr/README.md",
        "docs/adr/index.md",
        "docs/runbooks/recovery.md",
        "docs/specs/current-design.md",
    ],
)
def test_historical_command_does_not_exempt_current_documentation(
    tmp_path: Path, current_document: str
) -> None:
    """An accepted decision can retain a command that current docs must reject."""
    historical = tmp_path / "docs/adr/0001-retired-command.md"
    historical.parent.mkdir(parents=True)
    record = (
        "# ADR-0001: Former command\n\n"
        "- Status: Accepted\n\n"
        "## Decision\n\n"
        "Use `hephaestus-retired-command`.\n"
    )
    historical.write_text(record, encoding="utf-8")

    assert mod.check_docs_command_references(tmp_path, {"hephaestus-current"}) == []

    current = tmp_path / current_document
    current.parent.mkdir(parents=True, exist_ok=True)
    current.write_text(record, encoding="utf-8")

    assert mod.check_docs_command_references(tmp_path, {"hephaestus-current"}) == [
        f"{current_document}: references `hephaestus-retired-command` "
        "which is not in pyproject.toml [project.scripts]"
    ]


@pytest.mark.parametrize(
    "metadata",
    [
        "- Status: Draft",
        "- Status: Proposed",
        "- Status: Accepted\n- Status: Draft",
        "- Date: 2026-09-09",
    ],
)
def test_unaccepted_decision_still_requires_a_registered_command(
    tmp_path: Path, metadata: str
) -> None:
    """Body text cannot turn a current or unclassified decision into history."""
    decision = tmp_path / "docs/adr/0002-current-command.md"
    decision.parent.mkdir(parents=True)
    decision.write_text(
        f"# ADR-0002: Current command\n\n{metadata}\n\n"
        "## Decision\n\n"
        "- Status: Accepted\n\n"
        "Use `hephaestus-ghost-command`.\n",
        encoding="utf-8",
    )

    assert mod.check_docs_command_references(tmp_path, {"hephaestus-current"}) == [
        "docs/adr/0002-current-command.md: references `hephaestus-ghost-command` "
        "which is not in pyproject.toml [project.scripts]"
    ]


def test_docs_reference_check_scans_agents_md(tmp_path: Path) -> None:
    """The documentation guard treats AGENTS.md as an authoritative source."""
    (tmp_path / "README.md").write_text("", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text(
        "Run `hephaestus-ghost-command`.\n",
        encoding="utf-8",
    )

    assert mod.check_docs_command_references(tmp_path, {"hephaestus-real"}) == [
        "AGENTS.md: references `hephaestus-ghost-command` "
        "which is not in pyproject.toml [project.scripts]"
    ]


def test_precommit_hook_runs_when_agents_md_changes() -> None:
    """AGENTS.md edits must trigger the CLI sync guard."""
    config = yaml.safe_load((REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hook = next(
        h
        for repo in config["repos"]
        for h in repo.get("hooks", [])
        if h.get("id") == "check-cli-table-sync"
    )

    assert hook["entry"] == "uv run python -m hephaestus.scripts_lib.check_cli_table_sync"
    assert hook["language"] == "system"
    assert hook["pass_filenames"] is False
    assert hook["files"] == (
        r"^(README\.md|COMPATIBILITY\.md|AGENTS\.md|CLAUDE\.md|pyproject\.toml|docs/.*\.md)$"
    )
