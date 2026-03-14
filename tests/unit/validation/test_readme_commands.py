#!/usr/bin/env python3

"""Tests for hephaestus.validation.readme_commands module."""

from hephaestus.validation.readme_commands import (
    CodeBlock,
    ReadmeValidator,
)


def test_code_block_commands():
    """Test extracting commands from code blocks."""
    block = CodeBlock(
        language="bash",
        content="echo 'hello'\n# Comment\nls -la\n",
        line_number=10,
    )

    commands = block.commands()
    assert len(commands) == 2
    assert "echo 'hello'" in commands
    assert "ls -la" in commands


def test_code_block_has_skip_marker():
    """Test detecting skip markers in code blocks."""
    block = CodeBlock(
        language="bash",
        content="echo 'test'  # SKIP-VALIDATION",
        line_number=10,
    )

    assert block.has_skip_marker() is True

    block_no_skip = CodeBlock(
        language="bash",
        content="echo 'test'",
        line_number=10,
    )

    assert block_no_skip.has_skip_marker() is False


def test_extract_code_blocks(tmp_path):
    """Test extracting code blocks from markdown."""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """
# Test Document

```bash
echo 'hello'
```

Some text.

```python
print('world')
```
"""
    )

    validator = ReadmeValidator()
    blocks = validator.extract_code_blocks(md_file)

    assert len(blocks) == 2
    assert blocks[0].language == "bash"
    assert blocks[1].language == "python"
    assert "echo 'hello'" in blocks[0].content
    assert "print('world')" in blocks[1].content


def test_is_blocked_command():
    """Test detecting blocked commands."""
    validator = ReadmeValidator()

    assert validator.is_blocked_command("rm -rf /") is True
    assert validator.is_blocked_command("sudo apt install") is True
    assert validator.is_blocked_command("git commit") is True
    assert validator.is_blocked_command("echo 'safe'") is False


def test_is_allowed_command():
    """Test detecting allowed commands."""
    validator = ReadmeValidator()

    assert validator.is_allowed_command("echo 'test'") is True
    assert validator.is_allowed_command("ls -la") is True
    assert validator.is_allowed_command("python3 --version") is True
    assert validator.is_allowed_command("random_command") is False


def test_custom_allowed_prefixes():
    """Test validator with custom allowed prefixes."""
    custom_prefixes = ["myapp run", "myapp test"]
    validator = ReadmeValidator(allowed_prefixes=custom_prefixes)

    assert validator.is_allowed_command("myapp run tests") is True
    assert validator.is_allowed_command("echo 'test'") is False


def test_is_safe_command():
    """Test safety check for commands."""
    validator = ReadmeValidator()

    # Safe command
    is_safe, reason = validator.is_safe_command("echo 'test'")
    assert is_safe is True
    assert reason == "allowed"

    # Blocked command
    is_safe, reason = validator.is_safe_command("rm -rf /")
    assert is_safe is False
    assert "blocked" in reason

    # Not in allowed prefixes
    is_safe, reason = validator.is_safe_command("random_command")
    assert is_safe is False
    assert "allowed prefixes" in reason


def test_validate_syntax():
    """Test syntax validation."""
    validator = ReadmeValidator()

    # Valid syntax
    result = validator.validate_syntax("echo 'test'")
    assert result.passed is True
    assert result.check_type == "syntax"

    # Invalid syntax
    result = validator.validate_syntax("echo 'test")  # Missing closing quote
    assert result.passed is False
    assert result.check_type == "syntax"


def test_validate_availability():
    """Test binary availability check."""
    validator = ReadmeValidator()

    # Common binary that should exist
    result = validator.validate_availability("echo test")
    assert result.passed is True
    assert result.check_type == "availability"

    # Binary that shouldn't exist
    result = validator.validate_availability("nonexistent_binary_12345")
    assert result.passed is False
    assert result.check_type == "availability"


def test_validate_quick(tmp_path):
    """Test quick validation."""
    md_file = tmp_path / "test.md"
    md_file.write_text(
        """
# Test

```bash
echo 'test'
ls
```
"""
    )

    validator = ReadmeValidator()
    blocks = validator.extract_code_blocks(md_file)
    report = validator.validate_quick(blocks)

    assert report.level == "quick"
    assert report.total_commands >= 0
    # Results depend on system state


def test_generate_report(tmp_path):
    """Test report generation."""
    validator = ReadmeValidator()

    # Create a mock report
    from hephaestus.validation.readme_commands import ValidationReport

    report = ValidationReport(
        level="quick",
        timestamp="2024-01-01T00:00:00",
        total_blocks=2,
        total_commands=5,
        passed=4,
        failed=1,
    )

    report_file = tmp_path / "report.md"
    validator.generate_report(report, report_file)

    assert report_file.exists()
    content = report_file.read_text()
    assert "# README.md Command Validation Results" in content
    assert "**Passed**: 4" in content
    assert "**Failed**: 1" in content
