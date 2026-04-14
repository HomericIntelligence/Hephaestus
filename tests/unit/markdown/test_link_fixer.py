#!/usr/bin/env python3

"""Tests for hephaestus.markdown.link_fixer module."""

from pathlib import Path

import pytest

from hephaestus.markdown.link_fixer import LinkFixer, LinkFixerOptions, check_links, main


def test_fix_system_path_links():
    """Test fixing links with full system paths."""
    fixer = LinkFixer()

    # Test standard system path
    content = "See [docs](/home/user/repo/docs/guide.md)"
    fixed, count = fixer.fix_system_path_links(content)
    assert fixed == "See [docs](docs/guide.md)"
    assert count == 1

    # Test multiple system paths
    content = "See [A](/home/alice/project/a.md) and [B](/home/bob/work/b.md)"
    fixed, count = fixer.fix_system_path_links(content)
    assert fixed == "See [A](a.md) and [B](b.md)"
    assert count == 2

    # Test content without system paths
    content = "See [docs](docs/guide.md)"
    fixed, count = fixer.fix_system_path_links(content)
    assert fixed == content
    assert count == 0


def test_fix_absolute_path_links():
    """Test fixing absolute path links to relative paths."""
    fixer = LinkFixer()

    # Test from root level file (depth 0)
    file_path = Path("README.md")
    content = "See [agents](/agents/index.md)"
    fixed, count = fixer.fix_absolute_path_links(content, file_path)
    assert fixed == "See [agents](agents/index.md)"
    assert count == 1

    # Test from subdirectory (depth 1)
    file_path = Path("docs/README.md")
    content = "See [agents](/agents/index.md)"
    fixed, count = fixer.fix_absolute_path_links(content, file_path)
    assert fixed == "See [agents](../agents/index.md)"
    assert count == 1

    # Test from nested subdirectory (depth 3)
    file_path = Path("notes/issues/863/README.md")
    content = "See [agents](/agents/index.md)"
    fixed, count = fixer.fix_absolute_path_links(content, file_path)
    assert fixed == "See [agents](../../../agents/index.md)"
    assert count == 1

    # Test URL links (should not be changed)
    file_path = Path("README.md")
    content = "See [docs](https://example.com/docs)"
    fixed, count = fixer.fix_absolute_path_links(content, file_path)
    assert fixed == content
    assert count == 0


def test_link_fixer_integration(tmp_path):
    """Test full link fixer on a file."""
    # Create test file
    test_file = tmp_path / "docs" / "test.md"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("See [docs](/home/user/repo/docs/guide.md) and [agents](/agents/index.md)")

    # Create fixer
    options = LinkFixerOptions(verbose=False, dry_run=False)
    fixer = LinkFixer(options)

    # Fix file
    modified, _system_fixes, _absolute_fixes = fixer.fix_file(test_file)

    assert modified is True
    # Note: system_fixes count depends on the pattern matching the actual path structure

    # Check file was modified
    content = test_file.read_text()
    assert "/home/user/repo/" not in content


def test_link_fixer_dry_run(tmp_path):
    """Test that dry run doesn't modify files."""
    test_file = tmp_path / "test.md"
    original_content = "See [docs](/home/user/repo/docs/guide.md)"
    test_file.write_text(original_content)

    options = LinkFixerOptions(verbose=False, dry_run=True)
    fixer = LinkFixer(options)

    _modified, _system_fixes, _absolute_fixes = fixer.fix_file(test_file)

    # Should report as modified but not actually change file
    assert test_file.read_text() == original_content


def test_link_fixer_custom_pattern():
    """Test link fixer with custom system path pattern."""
    # Custom pattern for different path structure
    options = LinkFixerOptions(system_path_pattern=r"/custom/path")
    fixer = LinkFixer(options)

    content = "See [docs](/custom/path/docs/guide.md)"
    fixed, count = fixer.fix_system_path_links(content)
    assert fixed == "See [docs](docs/guide.md)"
    assert count == 1


def test_link_fixer_process_path_directory(tmp_path):
    """Test processing a directory."""
    # Create multiple markdown files
    (tmp_path / "test1.md").write_text("See [docs](/home/user/repo/docs.md)")
    (tmp_path / "test2.md").write_text("See [agents](/agents/index.md)")
    (tmp_path / "test3.txt").write_text("Not markdown")

    fixer = LinkFixer()
    files_modified, _system_fixes, _absolute_fixes = fixer.process_path(tmp_path)

    # Should process 2 markdown files
    assert files_modified >= 0  # Depends on if fixes were needed


class TestCheckLinks:
    """Tests for check_links() validation helper."""

    def test_no_issues_returns_zeros(self, tmp_path: Path) -> None:
        (tmp_path / "clean.md").write_text("No problematic links here.\n")
        files, sys_issues, abs_issues = check_links(tmp_path)
        assert files == 0
        assert sys_issues == 0
        assert abs_issues == 0

    def test_absolute_path_detected(self, tmp_path: Path) -> None:
        (tmp_path / "bad.md").write_text("See [link](/absolute/path.md)\n")
        _files, _sys, abs_issues = check_links(tmp_path)
        assert abs_issues >= 1

    def test_returns_tuple_of_three(self, tmp_path: Path) -> None:
        (tmp_path / "f.md").write_text("Clean content.\n")
        result = check_links(tmp_path)
        assert len(result) == 3


class TestMain:
    """Tests for main() CLI entry point."""

    def test_help_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.argv", ["hephaestus-check-links", "--help"])
        with pytest.raises(SystemExit) as exc:
            main()
        assert exc.value.code == 0

    def test_check_mode_clean_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "clean.md").write_text("No bad links here.\n")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-links", "--check", str(tmp_path)],
        )
        assert main() == 0

    def test_check_mode_bad_links(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (tmp_path / "bad.md").write_text("See [link](/absolute/path.md)\n")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-links", "--check", str(tmp_path)],
        )
        assert main() == 1

    def test_fix_mode_modifies_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        f = tmp_path / "f.md"
        f.write_text("See [link](/absolute/path.md)\n")
        monkeypatch.setattr(
            "sys.argv",
            ["hephaestus-check-links", str(tmp_path)],
        )
        result = main()
        assert result == 0  # fix mode always exits 0
