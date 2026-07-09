"""Tests for hephaestus/validation/api_table_docs.py."""

from __future__ import annotations

from pathlib import Path

from hephaestus.utils.helpers import get_repo_root
from hephaestus.validation.api_table_docs import (
    ApiTableFinding,
    find_violations,
    load_documented_symbols,
    main,
)

_CONFIG_DOC = """\
### `hephaestus.config`

| Symbol | Added | Notes |
|--------|-------|-------|
| `load_config` | 0.1.0 | Load YAML/JSON config file |
"""

_UTILS_DOC = """\
### `hephaestus.utils`

| Symbol | Added | Notes |
|--------|-------|-------|
| `slugify` | 0.1.0 | Convert text to URL-friendly slug |
"""

_BOTH_DOC = _CONFIG_DOC + "\n" + _UTILS_DOC


class TestLoadDocumentedSymbols:
    """Tests for load_documented_symbols()."""

    def test_parses_single_table(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(_CONFIG_DOC, encoding="utf-8")
        tables = load_documented_symbols(p)
        assert tables["hephaestus.config"] == {"load_config"}

    def test_parses_multiple_tables(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(_BOTH_DOC, encoding="utf-8")
        tables = load_documented_symbols(p)
        assert tables["hephaestus.config"] == {"load_config"}
        assert tables["hephaestus.utils"] == {"slugify"}

    def test_parses_nested_module_table(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.utils.git`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n"
            "| `run_git` | TBD | Execute Git commands |\n",
            encoding="utf-8",
        )
        tables = load_documented_symbols(p)
        assert tables["hephaestus.utils.git"] == {"run_git"}

    def test_skips_separator_rows(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.config`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n"  # separator — must be skipped
            "| `load_config` | 0.1.0 | x |\n",
            encoding="utf-8",
        )
        tables = load_documented_symbols(p)
        assert tables["hephaestus.config"] == {"load_config"}

    def test_stops_at_next_heading(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.config`\n"
            "| `load_config` | 0.1.0 | x |\n"
            "### `hephaestus.utils`\n"
            "| `slugify` | 0.1.0 | y |\n",
            encoding="utf-8",
        )
        tables = load_documented_symbols(p)
        assert "hephaestus.config" in tables
        assert "hephaestus.utils" in tables
        assert tables["hephaestus.config"] == {"load_config"}
        assert tables["hephaestus.utils"] == {"slugify"}

    def test_empty_doc_returns_empty(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text("# No tables here\n", encoding="utf-8")
        assert load_documented_symbols(p) == {}


class TestFindViolations:
    """Tests for find_violations()."""

    def test_table_not_found(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text("# no tables here\n", encoding="utf-8")
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.utils",))
        assert len(findings) == 1
        assert findings[0].kind == "table-not-found"
        assert "hephaestus.utils" in findings[0].detail

    def test_missing_from_docs_detected(self, tmp_path: Path) -> None:
        """A symbol in __all__ but absent from docs raises missing-from-docs."""
        p = tmp_path / "COMPATIBILITY.md"
        # Only one symbol documented, but hephaestus.config.__all__ has more
        p.write_text(
            "### `hephaestus.config`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n"
            "| `load_config` | 0.1.0 | x |\n",
            encoding="utf-8",
        )
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.config",))
        kinds = {f.kind for f in findings}
        assert "missing-from-docs" in kinds
        # validate_config is one of the symbols that was missing before this PR
        assert any("validate_config" in f.detail for f in findings)

    def test_missing_from_all_detected(self, tmp_path: Path) -> None:
        """A docs row with no corresponding __all__ symbol raises missing-from-all."""
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.config`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n"
            "| `nonexistent_symbol_xyz` | 0.1.0 | x |\n",
            encoding="utf-8",
        )
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.config",))
        kinds = {f.kind for f in findings}
        assert "missing-from-all" in kinds
        assert any("nonexistent_symbol_xyz" in f.detail for f in findings)

    def test_clean_when_aligned(self, tmp_path: Path) -> None:
        """No findings when documented symbols exactly match __all__."""
        import hephaestus.config as cfg

        doc_lines = "\n".join(f"| `{sym}` | 0.1.0 | x |" for sym in cfg.__all__)
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.config`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n" + doc_lines + "\n",
            encoding="utf-8",
        )
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.config",))
        assert findings == []

    def test_empty_packages_tuple_returns_no_findings(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text("# empty\n", encoding="utf-8")
        documented = load_documented_symbols(p)
        assert find_violations(documented, packages=()) == []

    def test_nested_module_missing_from_docs_detected(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text(
            "### `hephaestus.utils.git`\n\n"
            "| Symbol | Added | Notes |\n"
            "|--------|-------|-------|\n"
            "| `run_git` | TBD | Execute Git commands |\n",
            encoding="utf-8",
        )
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.utils.git",))
        assert any(f.kind == "missing-from-docs" for f in findings)
        assert any("git_ls_remote_contains" in f.detail for f in findings)

    def test_finding_is_dataclass(self, tmp_path: Path) -> None:
        p = tmp_path / "COMPATIBILITY.md"
        p.write_text("# no tables\n", encoding="utf-8")
        documented = load_documented_symbols(p)
        findings = find_violations(documented, packages=("hephaestus.utils",))
        assert isinstance(findings[0], ApiTableFinding)
        assert findings[0].package == "hephaestus.utils"


class TestLiveTreeAlignment:
    """Live guard: COMPATIBILITY.md must stay aligned with the actual __all__."""

    def test_real_compatibility_md_is_aligned(self) -> None:
        """After completing both tables, there must be ZERO drift for guarded packages."""
        root = get_repo_root()
        documented = load_documented_symbols(root / "COMPATIBILITY.md")
        findings = find_violations(documented)
        assert findings == [], "\n".join(f.detail for f in findings)


class TestMain:
    """Tests for the main() entry point."""

    def test_main_returns_zero_on_real_repo(self) -> None:
        assert main([]) == 0

    def test_main_returns_one_on_missing_table(self, tmp_path: Path) -> None:
        (tmp_path / "COMPATIBILITY.md").write_text("# no tables\n", encoding="utf-8")
        # Need a real repo structure for get_repo_root; override via --repo-root
        result = main(["--repo-root", str(tmp_path)])
        assert result == 1

    def test_main_json_output_is_valid_json(self, tmp_path: Path) -> None:
        import json

        (tmp_path / "COMPATIBILITY.md").write_text("# no tables\n", encoding="utf-8")
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            main(["--repo-root", str(tmp_path), "--json"])
        data = json.loads(buf.getvalue())
        assert "violations" in data
        assert isinstance(data["violations"], list)
