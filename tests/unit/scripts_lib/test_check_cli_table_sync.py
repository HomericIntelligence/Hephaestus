"""Tests for the README console-script documentation check."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.scripts_lib import check_cli_table_sync as mod


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
    monkeypatch.setattr(mod, "_readme_documented_commands", lambda: set())

    assert mod.main() == 1
    assert "hephaestus-missing" in capsys.readouterr().out


def test_main_passes_against_real_repository(capsys: pytest.CaptureFixture[str]) -> None:
    """The repository documents every currently packaged console script."""
    assert mod.main() == 0
    assert "OK" in capsys.readouterr().out
