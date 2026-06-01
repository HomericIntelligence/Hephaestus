"""Tests for hephaestus/validation/skill_catalog.py."""

from __future__ import annotations

import json
import textwrap
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from hephaestus.validation.skill_catalog import (
    check_skill_catalog,
    extract_skill_table_rows,
    main,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_table(tmp_path: Path, skill_names: list[str]) -> Path:
    """Write a minimal plugin-installation.md with one table row per skill."""
    rows = "\n".join(f"| {name} | `/cmd` | A skill |" for name in skill_names)
    content = textwrap.dedent(
        f"""\
        # Plugin

        ## What the Plugin Provides

        | Skill | Invocation | Description |
        |-------|-----------|-------------|
        {rows}
        """
    )
    path = tmp_path / "plugin-installation.md"
    path.write_text(content)
    return path


def make_skills_dir(tmp_path: Path, skill_names: list[str]) -> Path:
    """Create a skills/ directory with one subdir per name (each has SKILL.md)."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in skill_names:
        sub = skills_dir / name
        sub.mkdir()
        (sub / "SKILL.md").write_text(f"# {name}\n")
    return skills_dir


# ---------------------------------------------------------------------------
# extract_skill_table_rows
# ---------------------------------------------------------------------------


class TestExtractSkillTableRows:
    """Tests for extract_skill_table_rows."""

    def test_returns_empty_for_missing_file(self, tmp_path: Path) -> None:
        """Should return an empty set when the file does not exist."""
        assert extract_skill_table_rows(tmp_path / "missing.md") == set()

    def test_extracts_rows_from_simple_table(self, tmp_path: Path) -> None:
        """Should return the leftmost column for each data row."""
        path = make_table(tmp_path, ["alpha", "beta", "gamma"])
        assert extract_skill_table_rows(path) == {"alpha", "beta", "gamma"}

    def test_skips_header_row(self, tmp_path: Path) -> None:
        """Header row whose leftmost cell is ``Skill`` is not counted."""
        path = make_table(tmp_path, ["alpha"])
        result = extract_skill_table_rows(path)
        assert "Skill" not in result
        assert "alpha" in result

    def test_skips_divider_row(self, tmp_path: Path) -> None:
        """Divider rows (---) are not counted."""
        path = make_table(tmp_path, ["alpha", "beta"])
        result = extract_skill_table_rows(path)
        assert "---" not in result
        assert "-------" not in result

    def test_only_parses_first_table(self, tmp_path: Path) -> None:
        """Skills in a second table later in the file should be ignored."""
        content = textwrap.dedent(
            """\
            # Doc

            | Skill | Description |
            |-------|-------------|
            | alpha | first |

            ## Another section

            | Other | Field |
            |-------|-------|
            | beta | second |
            """
        )
        path = tmp_path / "two.md"
        path.write_text(content)
        result = extract_skill_table_rows(path)
        assert result == {"alpha"}

    def test_strips_backticks_from_skill_names(self, tmp_path: Path) -> None:
        """Backtick-wrapped skill names should be unwrapped."""
        content = textwrap.dedent(
            """\
            | Skill | Description |
            |-------|-------------|
            | `alpha` | A skill |
            """
        )
        path = tmp_path / "ticks.md"
        path.write_text(content)
        assert extract_skill_table_rows(path) == {"alpha"}


# ---------------------------------------------------------------------------
# check_skill_catalog
# ---------------------------------------------------------------------------


class TestCheckSkillCatalog:
    """Tests for check_skill_catalog."""

    def test_perfect_match_returns_empty_sets(self, tmp_path: Path) -> None:
        """Identical table and skills/ should return two empty sets."""
        table = make_table(tmp_path, ["alpha", "beta"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        missing, extra = check_skill_catalog(table, skills_dir)
        assert missing == set()
        assert extra == set()

    def test_detects_missing_skill_in_table(self, tmp_path: Path) -> None:
        """A shipped skill not in the table appears in ``missing``."""
        table = make_table(tmp_path, ["alpha", "beta"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta", "gamma"])
        missing, extra = check_skill_catalog(table, skills_dir)
        assert missing == {"gamma"}
        assert extra == set()

    def test_detects_extra_skill_in_table(self, tmp_path: Path) -> None:
        """A documented skill that is no longer shipped appears in ``extra``."""
        table = make_table(tmp_path, ["alpha", "beta", "removed"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        missing, extra = check_skill_catalog(table, skills_dir)
        assert missing == set()
        assert extra == {"removed"}

    def test_detects_both_missing_and_extra(self, tmp_path: Path) -> None:
        """Both kinds of drift can be reported together."""
        table = make_table(tmp_path, ["alpha", "removed"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "added"])
        missing, extra = check_skill_catalog(table, skills_dir)
        assert missing == {"added"}
        assert extra == {"removed"}

    def test_two_skills_table_three_skill_dirs(self, tmp_path: Path) -> None:
        """Explicit acceptance case from the audit finding."""
        table = make_table(tmp_path, ["alpha", "beta"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta", "gamma"])
        missing, extra = check_skill_catalog(table, skills_dir)
        assert missing == {"gamma"}
        assert extra == set()


# ---------------------------------------------------------------------------
# main — exit codes and output modes
# ---------------------------------------------------------------------------


class TestMainExitCodes:
    """main() returns the documented exit codes for sync/drift."""

    def test_returns_zero_when_complete(self, tmp_path: Path) -> None:
        """Main exits 0 when the table matches the skills directory."""
        table = make_table(tmp_path, ["alpha", "beta"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        result = main(["--table", str(table), "--skills-dir", str(skills_dir)])
        assert result == 0

    def test_returns_nonzero_when_missing(self, tmp_path: Path) -> None:
        """Main exits 1 when the table is missing a shipped skill."""
        table = make_table(tmp_path, ["alpha"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        result = main(["--table", str(table), "--skills-dir", str(skills_dir)])
        assert result == 1

    def test_returns_nonzero_when_extra(self, tmp_path: Path) -> None:
        """Main exits 1 when the table lists a skill that is not shipped."""
        table = make_table(tmp_path, ["alpha", "removed"])
        skills_dir = make_skills_dir(tmp_path, ["alpha"])
        result = main(["--table", str(table), "--skills-dir", str(skills_dir)])
        assert result == 1


class TestMainJsonOutput:
    """main(--json) emits a parseable JSON status envelope."""

    def test_json_ok_envelope(self, tmp_path: Path) -> None:
        """--json + sync produces {status: ok, exit_code: 0, ...}."""
        table = make_table(tmp_path, ["alpha"])
        skills_dir = make_skills_dir(tmp_path, ["alpha"])
        buf = StringIO()
        with patch("sys.stdout", buf):
            code = main(
                [
                    "--table",
                    str(table),
                    "--skills-dir",
                    str(skills_dir),
                    "--json",
                ]
            )
        payload = json.loads(buf.getvalue())
        assert code == 0
        assert payload["status"] == "ok"
        assert payload["exit_code"] == 0
        assert payload["missing"] == []
        assert payload["extra"] == []

    def test_json_error_envelope_lists_diff(self, tmp_path: Path) -> None:
        """--json + drift includes the missing/extra lists in the envelope."""
        table = make_table(tmp_path, ["alpha"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        buf = StringIO()
        with patch("sys.stdout", buf):
            code = main(
                [
                    "--table",
                    str(table),
                    "--skills-dir",
                    str(skills_dir),
                    "--json",
                ]
            )
        payload = json.loads(buf.getvalue())
        assert code == 1
        assert payload["status"] == "error"
        assert payload["exit_code"] == 1
        assert payload["missing"] == ["beta"]
        assert payload["extra"] == []


class TestMainTextOutput:
    """main() prints a human-readable diff in non-JSON mode."""

    def test_text_ok_message(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Sync run prints an OK message."""
        table = make_table(tmp_path, ["alpha"])
        skills_dir = make_skills_dir(tmp_path, ["alpha"])
        main(["--table", str(table), "--skills-dir", str(skills_dir)])
        captured = capsys.readouterr()
        assert "OK" in captured.out

    def test_text_error_lists_missing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Drift run prints the missing skill name."""
        table = make_table(tmp_path, ["alpha"])
        skills_dir = make_skills_dir(tmp_path, ["alpha", "beta"])
        main(["--table", str(table), "--skills-dir", str(skills_dir)])
        captured = capsys.readouterr()
        assert "beta" in captured.out
        assert "Missing" in captured.out
