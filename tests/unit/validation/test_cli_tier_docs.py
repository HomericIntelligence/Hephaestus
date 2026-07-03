"""Tests for hephaestus/validation/cli_tier_docs.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.utils.helpers import get_repo_root
from hephaestus.validation.cli_tier_docs import (
    find_duplicate_tiers,
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
        from hephaestus.validation.tiers import cli_tier_docs

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
        tiers, _, _ = load_documented_tiers(md)
        assert tiers == {"hephaestus-foo": "Stable", "hephaestus-bar": "Provisional"}

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
        tiers, _, _ = load_documented_tiers(md)
        assert tiers == {"hephaestus-foo": "Stable"}

    def test_load_tiers_missing_section_returns_empty(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text("# Just a doc\nNo section here.\n")
        tiers, _, _ = load_documented_tiers(md)
        assert tiers == {}


class TestDuplicateDetection:
    """Tests for find_duplicate_tiers() and the conflict-detection path."""

    def test_conflicting_tiers_are_flagged(self) -> None:
        v = find_duplicate_tiers({"hephaestus-foo": ["Stable", "Internal"]})
        assert len(v) == 1 and v[0].kind == "conflicting-tier"
        assert "Internal" in v[0].detail and "Stable" in v[0].detail

    def test_duplicate_consistent_tiers_are_flagged(self) -> None:
        v = find_duplicate_tiers({"hephaestus-foo": ["Stable", "Stable"]})
        assert len(v) == 1 and v[0].kind == "duplicate-tier"

    def test_single_occurrence_is_clean(self) -> None:
        assert find_duplicate_tiers({"hephaestus-foo": ["Stable"]}) == []

    def test_parser_preserves_all_occurrences(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier | Notes |\n"
            "|-----|------|-------|\n"
            "| `hephaestus-foo` | Stable | first |\n"
            "| `hephaestus-foo` | Internal | contradictory second |\n"
            "## Next\n"
        )
        tiers, occ, _ = load_documented_tiers(md)
        assert occ == {"hephaestus-foo": ["Stable", "Internal"]}
        assert tiers == {"hephaestus-foo": "Internal"}  # flattened: last-write-wins
        assert find_duplicate_tiers(occ)[0].kind == "conflicting-tier"

    def test_find_violations_surfaces_duplicates_when_aligned(self) -> None:
        """The contradiction is reported even when scripts/tiers align."""
        dups = find_duplicate_tiers({"hephaestus-foo": ["Stable", "Internal"]})
        v = find_violations({"hephaestus-foo": "x:y"}, {"hephaestus-foo": "Internal"}, dups)
        assert any(f.kind == "conflicting-tier" for f in v)


class TestCrossSectionDetection:
    """Tests for cross-section duplicate detection (issue #1257)."""

    def test_two_tier_sections_same_cli_conflict_flagged(self, tmp_path: Path) -> None:
        """Concrete repro from issue #1257: two sections, contradictory tiers."""
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Stable |\n"
            "\n## Public API\n\n"
            "Some text.\n\n"
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Internal |\n"
        )
        _tiers, occ, section_count = load_documented_tiers(md)
        assert occ == {"hephaestus-foo": ["Stable", "Internal"]}
        assert section_count == 2
        findings = find_duplicate_tiers(occ)
        assert any(f.kind == "conflicting-tier" for f in findings)

    def test_section_count_one_for_normal_doc(self, tmp_path: Path) -> None:
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Stable |\n"
        )
        _, _, section_count = load_documented_tiers(md)
        assert section_count == 1

    def test_duplicate_section_finding_emitted_by_main(self, tmp_path: Path) -> None:
        """main() emits duplicate-section when COMPATIBILITY.md has two tier sections."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n\n[project.scripts]\nhephaestus-foo = "pkg:main"\n'
        )
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Stable |\n"
            "\n## Other\n\n"
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Stable |\n"
        )
        result = main(["--repo-root", str(tmp_path)])
        assert result == 1  # non-zero exit due to duplicate-section

    def test_non_tier_h2_still_ends_section(self, tmp_path: Path) -> None:
        """Existing contract: rows under ## Other Section are NOT parsed."""
        md = tmp_path / "COMPATIBILITY.md"
        md.write_text(
            "## Console-Script Stability Tiers\n"
            "| CLI | Tier |\n"
            "|-----|------|\n"
            "| `hephaestus-foo` | Stable |\n"
            "## Other Section\n"
            "| `hephaestus-bar` | Internal |\n"
        )
        tiers, occ, section_count = load_documented_tiers(md)
        assert tiers == {"hephaestus-foo": "Stable"}
        assert "hephaestus-bar" not in occ
        assert section_count == 1


class TestRealRepo:
    """Live test: catches drift the moment a new [project.scripts] entry is added."""

    def test_repo_has_no_tier_doc_violations(self) -> None:
        assert main(["--repo-root", str(get_repo_root())]) == 0
