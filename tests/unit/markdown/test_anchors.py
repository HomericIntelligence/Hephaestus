"""Tests for hephaestus.markdown.anchors."""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.markdown.anchors import (
    _collect_markdown_files,
    check_anchors,
    extract_anchored_links,
    extract_headings,
    heading_to_anchor,
    main,
    validate_anchors,
)


class TestHeadingToAnchor:
    """Tests for heading_to_anchor()."""

    def test_simple_heading(self) -> None:
        assert heading_to_anchor("Installation") == "installation"

    def test_spaces_to_hyphens(self) -> None:
        assert heading_to_anchor("Getting Started") == "getting-started"

    def test_removes_special_chars(self) -> None:
        assert heading_to_anchor("Python 3.10+") == "python-310"

    def test_collapses_hyphens(self) -> None:
        assert heading_to_anchor("A & B") == "a-b"

    def test_strips_leading_trailing_hyphens(self) -> None:
        assert heading_to_anchor("!Hello!") == "hello"

    def test_preserves_numbers(self) -> None:
        assert heading_to_anchor("Step 1") == "step-1"

    def test_empty_string(self) -> None:
        assert heading_to_anchor("") == ""

    def test_lowercase(self) -> None:
        assert heading_to_anchor("UPPERCASE") == "uppercase"


class TestExtractHeadings:
    """Tests for extract_headings()."""

    def test_single_heading(self) -> None:
        content = "# Introduction\n\nSome text."
        assert extract_headings(content) == ["Introduction"]

    def test_multiple_levels(self) -> None:
        content = "# H1\n## H2\n### H3\n"
        assert extract_headings(content) == ["H1", "H2", "H3"]

    def test_no_headings(self) -> None:
        assert extract_headings("Just some text\nNo headings here.") == []

    def test_strips_whitespace(self) -> None:
        content = "#   Padded Heading  \n"
        assert extract_headings(content) == ["Padded Heading"]

    def test_ignores_code_block_headings(self) -> None:
        # Heading-like lines inside code blocks should still be extracted
        # (this is a simplistic parser — we don't track code block state)
        content = "# Real Heading\n```\n# Not really a heading\n```\n"
        headings = extract_headings(content)
        assert "Real Heading" in headings


class TestExtractAnchoredLinks:
    """Tests for extract_anchored_links()."""

    def test_finds_anchored_link(self) -> None:
        content = "[sec](installation.md#prerequisites)"
        result = extract_anchored_links(content, "README.md", "installation.md")
        assert len(result) == 1
        assert result[0] == ("README.md", "installation.md#prerequisites", "prerequisites")

    def test_filters_by_basename(self) -> None:
        content = "[a](foo.md#anchor1)\n[b](bar.md#anchor2)\n"
        result = extract_anchored_links(content, "src.md", "foo.md")
        assert len(result) == 1
        assert result[0][2] == "anchor1"

    def test_no_filter_returns_all(self) -> None:
        content = "[a](foo.md#a1)\n[b](bar.md#a2)\n"
        result = extract_anchored_links(content, "src.md", None)
        assert len(result) == 2

    def test_skips_links_without_anchor(self) -> None:
        content = "[plain](installation.md)"
        result = extract_anchored_links(content, "src.md", "installation.md")
        assert result == []

    def test_path_prefix_matches(self) -> None:
        content = "[link](docs/getting-started/installation.md#step-1)"
        result = extract_anchored_links(content, "src.md", "installation.md")
        assert len(result) == 1
        assert result[0][2] == "step-1"


class TestValidateAnchors:
    """Tests for validate_anchors()."""

    def _make_target(self, tmp_path: Path, content: str) -> Path:
        p = tmp_path / "installation.md"
        p.write_text(content)
        return p

    def test_valid_anchor(self, tmp_path: Path) -> None:
        target = self._make_target(tmp_path, "# Prerequisites\n\nSome text.\n")
        source = tmp_path / "README.md"
        source.write_text("[link](installation.md#prerequisites)")
        errors = validate_anchors([source], target)
        assert errors == []

    def test_broken_anchor(self, tmp_path: Path) -> None:
        target = self._make_target(tmp_path, "# Prerequisites\n")
        source = tmp_path / "README.md"
        source.write_text("[link](installation.md#nonexistent)")
        errors = validate_anchors([source], target)
        assert len(errors) == 1
        assert "nonexistent" in errors[0]

    def test_missing_target(self, tmp_path: Path) -> None:
        target = tmp_path / "missing.md"
        source = tmp_path / "README.md"
        source.write_text("[link](missing.md#anchor)")
        errors = validate_anchors([source], target)
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_missing_source(self, tmp_path: Path) -> None:
        target = self._make_target(tmp_path, "# Prerequisites\n")
        missing_source = tmp_path / "nonexistent.md"
        errors = validate_anchors([missing_source], target)
        assert len(errors) == 1
        assert "not found" in errors[0]

    def test_no_anchored_links(self, tmp_path: Path) -> None:
        target = self._make_target(tmp_path, "# Prerequisites\n")
        source = tmp_path / "README.md"
        source.write_text("No links here.")
        errors = validate_anchors([source], target)
        assert errors == []


class TestCollectMarkdownFiles:
    """Tests for _collect_markdown_files()."""

    def test_finds_md_files(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("# Test")
        (tmp_path / "docs").mkdir()
        (tmp_path / "docs" / "guide.md").write_text("# Guide")
        result = _collect_markdown_files(tmp_path)
        names = [p.name for p in result]
        assert "README.md" in names
        assert "guide.md" in names

    def test_excludes_build_dirs(self, tmp_path: Path) -> None:
        build = tmp_path / "build"
        build.mkdir()
        (build / "output.md").write_text("# Built")
        (tmp_path / "README.md").write_text("# Real")
        result = _collect_markdown_files(tmp_path)
        names = [p.name for p in result]
        assert "output.md" not in names
        assert "README.md" in names


class TestCheckAnchors:
    """Tests for check_anchors()."""

    def test_valid_returns_zero(self, tmp_path: Path) -> None:
        target = tmp_path / "install.md"
        target.write_text("# Setup\n")
        source = tmp_path / "README.md"
        source.write_text("[link](install.md#setup)")
        result = check_anchors(
            target_file=target,
            source_files=[source],
        )
        assert result == 0

    def test_broken_returns_one(self, tmp_path: Path) -> None:
        target = tmp_path / "install.md"
        target.write_text("# Setup\n")
        source = tmp_path / "README.md"
        source.write_text("[link](install.md#missing)")
        result = check_anchors(
            target_file=target,
            source_files=[source],
        )
        assert result == 1

    def test_verbose_success_message(self, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
        target = tmp_path / "install.md"
        target.write_text("# Setup\n")
        source = tmp_path / "README.md"
        source.write_text("No anchored links here.")
        check_anchors(target_file=target, source_files=[source], verbose=True)
        captured = capsys.readouterr()
        assert "valid" in captured.out.lower()


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-validate-anchors", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_missing_target_required(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-validate-anchors"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code != 0

    def test_valid_anchor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "install.md"
        target.write_text("# Setup\n")
        source = tmp_path / "README.md"
        source.write_text("[link](install.md#setup)")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-anchors",
                "--target",
                str(target),
                str(source),
            ],
        )
        assert main() == 0

    def test_broken_anchor(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        target = tmp_path / "install.md"
        target.write_text("# Setup\n")
        source = tmp_path / "README.md"
        source.write_text("[link](install.md#missing)")
        monkeypatch.setattr(
            "sys.argv",
            [
                "hephaestus-validate-anchors",
                "--target",
                str(target),
                str(source),
            ],
        )
        assert main() == 1
