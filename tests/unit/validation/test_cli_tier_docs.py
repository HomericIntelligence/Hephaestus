"""Tests for hephaestus/validation/cli_tier_docs.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.utils.helpers import get_repo_root
from hephaestus.validation.cli_tier_docs import (
    find_violations,
    load_documented_tiers,
    load_pyproject_scripts,
    main,
)


class TestFindViolations:
    """Tests for the find_violations() cross-check function."""

    def test_no_findings_when_aligned(self) -> None:
        assert (
            find_violations({"hephaestus-foo": "pkg.mod:main"}, {"hephaestus-foo": "Stable"}) == []
        )

    def test_missing_from_docs(self) -> None:
        v = find_violations(
            {"hephaestus-foo": "pkg.mod:main"}, {"hephaestus-foo-other": "Internal"}
        )
        assert len(v) == 2  # one missing-from-docs, one missing-from-pyproject
        kinds = sorted(f.kind for f in v)
        assert kinds == ["missing-from-docs", "missing-from-pyproject"]

    def test_invalid_tier_value(self) -> None:
        v = find_violations({"hephaestus-foo": "x:y"}, {"hephaestus-foo": "Banana"})
        assert len(v) == 1 and v[0].kind == "invalid-tier"

    def test_parser_found_no_rows_guards_silent_failure(self) -> None:
        """Guard from Decision 4: scripts present + empty tiers fails loudly."""
        v = find_violations({"hephaestus-foo": "x:y"}, {})
        assert len(v) == 1 and v[0].kind == "parser-found-no-rows"

    def test_empty_pyproject_empty_docs_no_findings(self) -> None:
        assert find_violations({}, {}) == []


class TestParsing:
    """Tests for load_pyproject_scripts() and load_documented_tiers()."""

    def test_load_scripts_parses_pyproject(self, tmp_path: Path) -> None:
        p = tmp_path / "pyproject.toml"
        p.write_text('[project.scripts]\nhephaestus-foo = "pkg.mod:main"\n')
        assert load_pyproject_scripts(p) == {"hephaestus-foo": "pkg.mod:main"}

    def test_load_scripts_raises_on_missing_tomllib(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Guard from Decision 5: RuntimeError when tomli/tomllib is missing."""
        from hephaestus.validation import cli_tier_docs

        monkeypatch.setattr(cli_tier_docs, "import_tomllib", lambda: None)

        p = tmp_path / "pyproject.toml"
        p.write_text('[project.scripts]\nhephaestus-foo = "pkg.mod:main"\n')

        with pytest.raises(
            RuntimeError,
            match=r"tomllib.*tomli.*required",
        ):
            load_pyproject_scripts(p)

    def test_load_tiers_skips_separator_rows(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n\n"
            "Some preamble.\n\n"
            "| CLI | Tier | Notes |\n"
            "|-----|------|-------|\n"  # separator row — must be skipped
            "| `hephaestus-foo` | Stable | A note |\n"
            "| `hephaestus-bar` | Provisional | Another |\n"
            "\n## Next Section\n"
        )
        result = load_documented_tiers(md)
        assert result == {"hephaestus-foo": "Stable", "hephaestus-bar": "Provisional"}

    def test_load_tiers_stops_at_next_section(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier | Notes |\n"
            "|-----|------|-------|\n"
            "| `hephaestus-foo` | Stable | x |\n"
            "## Other Section\n"
            "| `hephaestus-bar` | Internal | y |\n"  # NOT in scope
        )
        assert load_documented_tiers(md) == {"hephaestus-foo": "Stable"}

    def test_load_tiers_missing_section_returns_empty(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text("# Just a doc\nNo section here.\n")
        assert load_documented_tiers(md) == {}


class TestRealRepo:
    """Live test: catches drift the moment a new [project.scripts] entry is added."""

    def test_repo_has_no_tier_doc_violations(self) -> None:
        assert main(["--repo-root", str(get_repo_root())]) == 0
