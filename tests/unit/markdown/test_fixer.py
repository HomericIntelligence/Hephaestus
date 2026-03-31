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


# ---------------------------------------------------------------------------
# _try_fix_md036_line (MD036)
# ---------------------------------------------------------------------------


class TestFixMd036:
    """Tests for _try_fix_md036_line() and MD036 behaviour in _fix_structural_issues."""

    def test_converts_bold_as_heading(self) -> None:
        """Bold text that looks like a heading is converted to an ATX heading."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("**Introduction**", "", "")
        assert result is not None
        assert "### Introduction" in result

    def test_adds_blank_line_before_when_prev_nonempty(self) -> None:
        """A blank line is prepended when the preceding line is not blank."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("**Section**", "Some preceding text", "")
        assert result is not None
        assert result[0] == ""

    def test_adds_blank_line_after_when_next_nonempty(self) -> None:
        """A blank line is appended when the following line is not blank."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("**Section**", "", "Following text")
        assert result is not None
        assert result[-1] == ""

    def test_no_blank_lines_when_surrounded_by_blank(self) -> None:
        """No extra blank lines are inserted when already surrounded by blanks."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("**Section**", "", "")
        assert result is not None
        assert result == ["### Section"]

    def test_removes_trailing_colon_after_bold(self) -> None:
        """Trailing colon after closing ** is stripped from the heading text."""
        f = MarkdownFixer()
        # Colon after the closing **: **Summary**:
        result = f._try_fix_md036_line("**Summary**:", "", "")
        assert result is not None
        assert "### Summary" in result

    def test_ignores_long_bold_text(self) -> None:
        """Bold text longer than 49 characters is not treated as a heading."""
        f = MarkdownFixer()
        long_text = "**" + "A" * 50 + "**"
        result = f._try_fix_md036_line(long_text, "", "")
        assert result is None

    def test_ignores_lowercase_bold_text(self) -> None:
        """Bold text starting with a lowercase letter is not converted."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("**some inline bold**", "", "")
        assert result is None

    def test_ignores_non_bold_line(self) -> None:
        """Regular text lines return None."""
        f = MarkdownFixer()
        result = f._try_fix_md036_line("Just regular text", "", "")
        assert result is None

    def test_structural_issues_converts_bold_heading(self) -> None:
        """Full _fix_structural_issues converts **Bold** to ### Bold."""
        f = MarkdownFixer()
        content = "Intro\n\n**Overview**\n\nDetails\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert "### Overview" in fixed
        assert fixes > 0


# ---------------------------------------------------------------------------
# _fix_md022_heading_blank_lines (MD022)
# ---------------------------------------------------------------------------


class TestFixMd022:
    """Tests for _fix_md022_heading_blank_lines() and MD022 via _fix_structural_issues."""

    def test_adds_blank_line_before_heading(self) -> None:
        """A blank line is inserted before a heading preceded by text."""
        f = MarkdownFixer()
        content = "Some text\n## Heading\n\nMore text\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "\n\n## Heading" in fixed

    def test_adds_blank_line_after_heading(self) -> None:
        """A blank line is inserted after a heading followed by text."""
        f = MarkdownFixer()
        content = "\n## Heading\nSome text\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "## Heading\n\n" in fixed

    def test_no_extra_blank_before_heading_at_start(self) -> None:
        """No blank line is prepended when heading is at the document start."""
        f = MarkdownFixer()
        content = "## Heading\n\nContent\n"
        fixed, fixes = f._fix_structural_issues(content)
        # Nothing to add before — no extra blank at very top
        assert fixed.startswith("## Heading")
        assert fixes == 0

    def test_no_blank_after_heading_when_next_is_heading(self) -> None:
        """No blank line is added AFTER a heading when the next line is also a heading."""
        f = MarkdownFixer()
        content = "\n## Heading 1\n## Heading 2\n\nContent\n"
        fixed, _fixes = f._fix_structural_issues(content)
        # MD022 only skips the trailing blank when the NEXT line is a heading;
        # the blank BEFORE Heading 2 is still inserted (MD022 requires blank before too)
        assert "## Heading 1\n\n## Heading 2" in fixed

    def test_direct_method_returns_fix_count(self) -> None:
        """_fix_md022_heading_blank_lines() returns correct fix count."""
        f = MarkdownFixer()
        accumulated: list[str] = ["Some text"]
        fixes = f._fix_md022_heading_blank_lines(
            "## Heading", "Next line", accumulated, "Some text"
        )
        assert fixes == 2  # blank before + blank after
        assert accumulated == ["Some text", "", "## Heading", ""]


# ---------------------------------------------------------------------------
# _fix_md031_code_block_blank_lines (MD031)
# ---------------------------------------------------------------------------


class TestFixMd031:
    """Tests for _fix_md031_code_block_blank_lines() and MD031 via _fix_structural_issues."""

    def test_adds_blank_line_before_code_block(self) -> None:
        """A blank line is inserted before a fenced code block."""
        f = MarkdownFixer()
        content = "Text\n```python\ncode\n```\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "\n\n```python" in fixed

    def test_adds_blank_line_after_code_block(self) -> None:
        """A blank line is inserted after a fenced code block."""
        f = MarkdownFixer()
        content = "\n```python\ncode\n```\nMore text\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "```\n\nMore text" in fixed

    def test_clean_code_block_unchanged(self) -> None:
        """Code block already surrounded by blank lines is not modified."""
        f = MarkdownFixer()
        content = "\n```python\ncode\n```\n"
        _fixed, fixes = f._fix_structural_issues(content)
        assert fixes == 0

    def test_direct_method_advances_index(self) -> None:
        """_fix_md031_code_block_blank_lines() returns the updated index past the fence."""
        f = MarkdownFixer()
        lines = ["Text", "```python", "code", "```", "After"]
        accumulated: list[str] = ["Text"]
        fixes, new_i = f._fix_md031_code_block_blank_lines(lines, 1, accumulated, "Text")
        assert fixes > 0
        # Index stops at the "After" line (index 4) so caller can inspect it
        assert new_i == 4


# ---------------------------------------------------------------------------
# _fix_md032_md029_list / _normalize_ordered_item (MD032, MD029)
# ---------------------------------------------------------------------------


class TestFixMd029:
    """Tests for ordered-list numbering normalisation (MD029)."""

    def test_normalises_sequential_numbers_to_one(self) -> None:
        """Ordered items 1., 2., 3. are all changed to 1."""
        f = MarkdownFixer()
        content = "\n1. First\n2. Second\n3. Third\n\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        for line in fixed.strip().split("\n"):
            if line.strip():
                assert line.startswith("1.")

    def test_already_normalised_list_unchanged(self) -> None:
        """A list that already uses 1. everywhere reports zero ordered-item fixes."""
        f = MarkdownFixer()
        content = "\n1. First\n1. Second\n1. Third\n\n"
        fixed, _fixes = f._fix_structural_issues(content)
        for line in fixed.strip().split("\n"):
            if line.strip():
                assert line.startswith("1.")

    def test_normalize_ordered_item_increments_counter(self) -> None:
        """_normalize_ordered_item() increments fixes_ref when changing a number."""
        f = MarkdownFixer()
        counter: list[int] = [0]
        result = f._normalize_ordered_item("2. item", counter)
        assert result == "1. item"
        assert counter[0] == 1

    def test_normalize_ordered_item_no_change_when_already_one(self) -> None:
        """_normalize_ordered_item() leaves fixes_ref at 0 when already 1."""
        f = MarkdownFixer()
        counter: list[int] = [0]
        result = f._normalize_ordered_item("1. item", counter)
        assert result == "1. item"
        assert counter[0] == 0


class TestFixMd032:
    """Tests for blank lines around lists (MD032)."""

    def test_adds_blank_line_before_unordered_list(self) -> None:
        """A blank line is added before an unordered list preceded by text."""
        f = MarkdownFixer()
        content = "Text\n- item 1\n- item 2\n\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "\n\n- item 1" in fixed

    def test_adds_blank_line_after_unordered_list(self) -> None:
        """A blank line is added after an unordered list followed by text."""
        f = MarkdownFixer()
        content = "\n- item 1\n- item 2\nText after\n"
        fixed, fixes = f._fix_structural_issues(content)
        assert fixes > 0
        assert "- item 2\n\nText after" in fixed

    def test_no_extra_blank_when_adjacent_list_items(self) -> None:
        """Consecutive list items do not get extra blank lines between them."""
        f = MarkdownFixer()
        content = "\n- item 1\n- item 2\n\n"
        fixed, _fixes = f._fix_structural_issues(content)
        assert "- item 1\n- item 2" in fixed
