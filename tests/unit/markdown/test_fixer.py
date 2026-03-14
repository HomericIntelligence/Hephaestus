#!/usr/bin/env python3

"""Tests for hephaestus.markdown.fixer module."""

from hephaestus.markdown.fixer import FixerOptions, MarkdownFixer


def test_fix_md034_bare_urls():
    """Test that bare URLs are wrapped in angle brackets."""
    fixer = MarkdownFixer()

    # Test simple bare URL
    content = "Check out https://example.com for more info."
    fixed, count = fixer._fix_md034_bare_urls(content)
    assert fixed == "Check out <https://example.com> for more info."
    assert count == 1

    # Test multiple URLs
    content = "Visit https://example.com and http://test.org"
    fixed, count = fixer._fix_md034_bare_urls(content)
    assert fixed == "Visit <https://example.com> and <http://test.org>"
    assert count == 2

    # Test URL in markdown link (should not be wrapped)
    content = "See [documentation](https://example.com) for details."
    fixed, count = fixer._fix_md034_bare_urls(content)
    assert fixed == "See [documentation](https://example.com) for details."
    assert count == 0

    # Test mixed content
    content = "Bare url https://example.com and [link](https://test.org) together"
    fixed, count = fixer._fix_md034_bare_urls(content)
    assert fixed == "Bare url <https://example.com> and [link](https://test.org) together"
    assert count == 1


def test_fix_md040_code_language():
    """Test that bare ``` lines (no language tag) are replaced with ```text."""
    fixer = MarkdownFixer()

    # A bare ``` line (opening fence without a language specifier) should be fixed
    content = "```\ncode here\n```\n"
    fixed, _count = fixer._fix_md040_code_language(content)
    assert "```text" in fixed

    # A code block that already has a language tag should NOT have its opening fence changed
    content_with_lang = "```python\ncode here\n```\n"
    fixed2, _count2 = fixer._fix_md040_code_language(content_with_lang)
    assert "```python" in fixed2


def test_fix_md012_multiple_blank_lines():
    """Test that multiple blank lines are reduced to two."""
    fixer = MarkdownFixer()

    content = "Line 1\n\n\n\nLine 2"
    fixed, count = fixer._fix_md012_multiple_blank_lines(content)
    assert fixed == "Line 1\n\nLine 2"
    assert count > 0


def test_is_list_item_ordered():
    """Test that ordered list items are detected correctly."""
    fixer = MarkdownFixer()

    assert fixer._is_list_item("1. First item")
    assert fixer._is_list_item("2. Second item")
    assert fixer._is_list_item("  1. Indented item")
    assert not fixer._is_list_item("1\\. Not a list item (escaped)")
    assert not fixer._is_list_item("just text")


def test_fix_md026_heading_punctuation():
    """Test that trailing punctuation is removed from headings."""
    fixer = MarkdownFixer()

    content = "# Heading:\n\n## Another heading.\n\nText"
    fixed, count = fixer._fix_md026_heading_punctuation(content)
    assert "# Heading\n" in fixed
    assert "## Another heading\n" in fixed
    assert count == 2


def test_markdown_fixer_integration(tmp_path):
    """Test full markdown fixer on a file."""
    # Create test file
    test_file = tmp_path / "test.md"
    test_file.write_text(
        "# Test Document:\n\n\n\nVisit https://example.com for info.\n\n```\ncode block\n```\n"
    )

    # Create fixer with verbose off
    options = FixerOptions(verbose=False, dry_run=False)
    fixer = MarkdownFixer(options)

    # Fix file
    modified, fixes = fixer.fix_file(test_file)

    assert modified is True
    assert fixes > 0

    # Check file was actually modified
    content = test_file.read_text()
    assert "# Test Document\n" in content  # Heading punctuation removed
    assert "<https://example.com>" in content  # Bare URL wrapped


def test_markdown_fixer_dry_run(tmp_path):
    """Test that dry run doesn't modify files."""
    test_file = tmp_path / "test.md"
    original_content = "# Test:\n\nhttps://example.com"
    test_file.write_text(original_content)

    options = FixerOptions(verbose=False, dry_run=True)
    fixer = MarkdownFixer(options)

    modified, _fixes = fixer.fix_file(test_file)

    # Should report as modified but not actually change file
    assert modified is True
    assert test_file.read_text() == original_content


def test_markdown_fixer_process_path_file(tmp_path):
    """Test processing a single file."""
    test_file = tmp_path / "test.md"
    test_file.write_text("# Test:\n\nhttps://example.com")

    fixer = MarkdownFixer()
    files_modified, total_fixes = fixer.process_path(test_file)

    assert files_modified == 1
    assert total_fixes > 0


def test_markdown_fixer_process_path_directory(tmp_path):
    """Test processing a directory."""
    # Create multiple markdown files
    (tmp_path / "test1.md").write_text("# Test 1:\n")
    (tmp_path / "test2.md").write_text("# Test 2:\n")
    (tmp_path / "test3.txt").write_text("Not markdown")

    fixer = MarkdownFixer()
    files_modified, total_fixes = fixer.process_path(tmp_path)

    # Should process 2 markdown files
    assert files_modified == 2
    assert total_fixes > 0


def test_markdown_fixer_excludes_directories(tmp_path):
    """Test that excluded directories are skipped."""
    # Create files in excluded directory
    excluded = tmp_path / ".git"
    excluded.mkdir()
    (excluded / "test.md").write_text("# Test:\n")

    # Create file in normal directory
    (tmp_path / "normal.md").write_text("# Normal:\n")

    fixer = MarkdownFixer()
    files_modified, _total_fixes = fixer.process_path(tmp_path)

    # Should only process normal.md
    assert files_modified == 1


def test_markdown_fixer_process_path_nonexistent(tmp_path):
    """Test processing a non-existent path."""
    fixer = MarkdownFixer()
    files_modified, total_fixes = fixer.process_path(tmp_path / "nonexistent")
    assert files_modified == 0
    assert total_fixes == 0


def test_markdown_fixer_process_path_non_md_file(tmp_path):
    """Test processing a non-markdown file."""
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("not markdown")
    fixer = MarkdownFixer()
    files_modified, total_fixes = fixer.process_path(txt_file)
    assert files_modified == 0
    assert total_fixes == 0


def test_markdown_fixer_no_markdown_in_dir(tmp_path):
    """Test processing a directory with no markdown files."""
    (tmp_path / "test.txt").write_text("plain text")
    fixer = MarkdownFixer()
    files_modified, total_fixes = fixer.process_path(tmp_path)
    assert files_modified == 0
    assert total_fixes == 0


def test_fix_structural_issues_heading_blank_lines():
    """Headings get blank lines added around them."""
    fixer = MarkdownFixer()
    content = "Some text\n## Heading\nMore text\n"
    fixed, fixes = fixer._fix_structural_issues(content)
    assert fixes > 0
    assert "\n\n## Heading\n\n" in fixed


def test_fix_structural_issues_code_block_blank_lines():
    """Code blocks get blank lines added around them."""
    fixer = MarkdownFixer()
    content = "Some text\n```python\ncode\n```\nMore text\n"
    fixed, fixes = fixer._fix_structural_issues(content)
    assert fixes > 0
    lines = fixed.split("\n")
    # There should be a blank line before the code block
    fence_idx = next(i for i, line in enumerate(lines) if line.strip() == "```python")
    assert lines[fence_idx - 1] == ""


def test_fix_structural_issues_unordered_list_blank_lines():
    """Unordered lists get blank lines around them."""
    fixer = MarkdownFixer()
    content = "Text before\n- item 1\n- item 2\nText after\n"
    _fixed, fixes = fixer._fix_structural_issues(content)
    assert fixes > 0


def test_fix_structural_issues_ordered_list():
    """Ordered list items are processed."""
    fixer = MarkdownFixer()
    content = "Text\n\n1. First\n2. Second\n3. Third\n\nMore text\n"
    fixed, _fixes = fixer._fix_structural_issues(content)
    # Ordered list items should be present
    assert "1." in fixed
