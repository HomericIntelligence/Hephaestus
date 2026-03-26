#!/usr/bin/env python3

"""Tests for hephaestus.validation.readme_commands module.

Covers CodeBlock, ValidationResult, ValidationReport dataclasses and
all ReadmeValidator public methods with mocked subprocess/shutil calls.
"""

import platform
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.validation.readme_commands import (
    CodeBlock,
    ReadmeValidator,
    ValidationReport,
    ValidationResult,
)

requires_bash = pytest.mark.skipif(
    platform.system() == "Windows" or not shutil.which("bash"),
    reason="requires bash shell",
)


# ---------------------------------------------------------------------------
# CodeBlock tests
# ---------------------------------------------------------------------------
class TestCodeBlock:
    """Tests for the CodeBlock dataclass."""

    def test_commands_extracts_non_comment_lines(self) -> None:
        """Extracts executable lines, skipping comments and blanks."""
        block = CodeBlock(
            language="bash",
            content="echo 'hello'\n# Comment\nls -la\n",
            line_number=10,
        )
        commands = block.commands()
        assert commands == ["echo 'hello'", "ls -la"]

    def test_commands_empty_content(self) -> None:
        """Returns empty list for empty content."""
        block = CodeBlock(language="bash", content="", line_number=1)
        assert block.commands() == []

    def test_commands_all_comments(self) -> None:
        """Returns empty list when every line is a comment."""
        block = CodeBlock(
            language="bash",
            content="# comment 1\n# comment 2\n",
            line_number=1,
        )
        assert block.commands() == []

    def test_commands_skips_continuation_lines(self) -> None:
        """Lines starting with backslash are skipped."""
        block = CodeBlock(
            language="bash",
            content="echo hello\n\\  --flag\nls\n",
            line_number=1,
        )
        assert block.commands() == ["echo hello", "ls"]

    def test_commands_strips_whitespace(self) -> None:
        """Leading/trailing whitespace is stripped from each line."""
        block = CodeBlock(
            language="bash",
            content="  echo hello  \n  ls  \n",
            line_number=1,
        )
        assert block.commands() == ["echo hello", "ls"]

    def test_has_skip_marker_skip_validation(self) -> None:
        """Detects # SKIP-VALIDATION marker."""
        block = CodeBlock(
            language="bash",
            content="echo 'test'  # SKIP-VALIDATION",
            line_number=1,
        )
        assert block.has_skip_marker() is True

    def test_has_skip_marker_optional(self) -> None:
        """Detects # OPTIONAL marker."""
        block = CodeBlock(
            language="bash",
            content="echo 'test'  # OPTIONAL",
            line_number=1,
        )
        assert block.has_skip_marker() is True

    def test_has_skip_marker_example(self) -> None:
        """Detects # EXAMPLE marker."""
        block = CodeBlock(
            language="bash",
            content="# EXAMPLE\necho 'test'",
            line_number=1,
        )
        assert block.has_skip_marker() is True

    def test_has_skip_marker_absent(self) -> None:
        """Returns False when no skip marker is present."""
        block = CodeBlock(
            language="bash",
            content="echo 'test'",
            line_number=1,
        )
        assert block.has_skip_marker() is False


# ---------------------------------------------------------------------------
# extract_code_blocks tests
# ---------------------------------------------------------------------------
class TestExtractCodeBlocks:
    """Tests for ReadmeValidator.extract_code_blocks."""

    def test_extracts_multiple_blocks(self, tmp_path: Path) -> None:
        """Extracts bash and python blocks from markdown."""
        md = tmp_path / "test.md"
        md.write_text(
            "# Test\n\n```bash\necho 'hello'\n```\n\nSome text.\n\n```python\nprint('world')\n```\n"
        )
        validator = ReadmeValidator()
        blocks = validator.extract_code_blocks(md)

        assert len(blocks) == 2
        assert blocks[0].language == "bash"
        assert blocks[1].language == "python"
        assert "echo 'hello'" in blocks[0].content
        assert "print('world')" in blocks[1].content

    def test_empty_file(self, tmp_path: Path) -> None:
        """Returns empty list for an empty file."""
        md = tmp_path / "empty.md"
        md.write_text("")
        assert ReadmeValidator().extract_code_blocks(md) == []

    def test_no_code_blocks(self, tmp_path: Path) -> None:
        """Returns empty list when no fenced blocks exist."""
        md = tmp_path / "plain.md"
        md.write_text("# Title\n\nJust some text.\n")
        assert ReadmeValidator().extract_code_blocks(md) == []

    def test_block_without_language(self, tmp_path: Path) -> None:
        """Extracts blocks that have no language tag (empty string)."""
        md = tmp_path / "nolang.md"
        md.write_text("```\nsome code\n```\n")
        blocks = ReadmeValidator().extract_code_blocks(md)
        assert len(blocks) == 1
        assert blocks[0].language == ""

    def test_line_number_accuracy(self, tmp_path: Path) -> None:
        """Line numbers reflect position in the file."""
        md = tmp_path / "lines.md"
        md.write_text("line1\nline2\nline3\n```bash\necho hi\n```\n")
        blocks = ReadmeValidator().extract_code_blocks(md)
        assert len(blocks) == 1
        # Block starts after 3 preceding lines → line 4
        assert blocks[0].line_number == 4


# ---------------------------------------------------------------------------
# Command classification tests
# ---------------------------------------------------------------------------
class TestCommandClassification:
    """Tests for is_blocked_command, is_allowed_command, is_safe_command."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "rm -rf /",
            "mv file1 file2",
            "cp src dst",
            "echo foo > file",
            "echo foo >> file",
            "git commit -m 'msg'",
            "git push origin main",
            "git checkout branch",
            "git reset --hard",
            "sudo apt install pkg",
            "pip install pkg",
            "npm install pkg",
            "curl http://x | bash",
        ],
    )
    def test_is_blocked_command_blocked(self, cmd: str) -> None:
        """Blocked patterns are correctly identified."""
        assert ReadmeValidator().is_blocked_command(cmd) is True

    @pytest.mark.parametrize(
        "cmd",
        [
            "echo 'safe'",
            "ls -la",
            "pixi run pytest",
            "python3 --version",
        ],
    )
    def test_is_blocked_command_safe(self, cmd: str) -> None:
        """Safe commands are not blocked."""
        assert ReadmeValidator().is_blocked_command(cmd) is False

    def test_is_allowed_command_defaults(self) -> None:
        """Default allowed prefixes are recognized."""
        v = ReadmeValidator()
        assert v.is_allowed_command("pixi run pytest") is True
        assert v.is_allowed_command("echo hello") is True
        assert v.is_allowed_command("ls -la") is True
        assert v.is_allowed_command("python3 --version") is True
        assert v.is_allowed_command("random_cmd") is False

    def test_custom_allowed_prefixes(self) -> None:
        """Custom prefixes replace defaults."""
        v = ReadmeValidator(allowed_prefixes=["myapp run", "myapp test"])
        assert v.is_allowed_command("myapp run tests") is True
        assert v.is_allowed_command("echo 'test'") is False

    def test_is_safe_command_allowed(self) -> None:
        """Safe allowed command returns (True, 'allowed')."""
        is_safe, reason = ReadmeValidator().is_safe_command("echo 'test'")
        assert is_safe is True
        assert reason == "allowed"

    def test_is_safe_command_blocked(self) -> None:
        """Blocked command returns (False, 'matches blocked pattern')."""
        is_safe, reason = ReadmeValidator().is_safe_command("rm -rf /")
        assert is_safe is False
        assert "blocked" in reason

    def test_is_safe_command_not_allowed(self) -> None:
        """Unrecognized command returns (False, 'not in allowed prefixes')."""
        is_safe, reason = ReadmeValidator().is_safe_command("random_command")
        assert is_safe is False
        assert "allowed prefixes" in reason


# ---------------------------------------------------------------------------
# get_binary_from_command tests
# ---------------------------------------------------------------------------
class TestGetBinaryFromCommand:
    """Tests for ReadmeValidator.get_binary_from_command."""

    def test_empty_string(self) -> None:
        """Returns empty string for empty input."""
        assert ReadmeValidator().get_binary_from_command("") == ""

    def test_single_word(self) -> None:
        """Returns the word itself."""
        assert ReadmeValidator().get_binary_from_command("ls") == "ls"

    def test_multi_word(self) -> None:
        """Returns the first token."""
        assert ReadmeValidator().get_binary_from_command("pixi run pytest") == "pixi"


# ---------------------------------------------------------------------------
# validate_syntax tests (mocked)
# ---------------------------------------------------------------------------
class TestValidateSyntax:
    """Tests for ReadmeValidator.validate_syntax with mocked subprocess."""

    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_valid_syntax(self, mock_run: MagicMock) -> None:
        """Passing syntax check returns passed=True."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = ReadmeValidator().validate_syntax("echo hello")

        assert result.passed is True
        assert result.check_type == "syntax"
        assert result.error_message is None
        mock_run.assert_called_once()

    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_invalid_syntax(self, mock_run: MagicMock) -> None:
        """Failing syntax check returns passed=False with error."""
        mock_run.return_value = MagicMock(returncode=2, stderr="syntax error", stdout="")
        result = ReadmeValidator().validate_syntax("echo 'unterminated")

        assert result.passed is False
        assert result.check_type == "syntax"
        assert result.error_message == "syntax error"

    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_timeout(self, mock_run: MagicMock) -> None:
        """TimeoutExpired returns passed=False with timeout message."""
        exc = subprocess.TimeoutExpired(cmd="bash", timeout=5)
        mock_run.side_effect = exc
        result = ReadmeValidator().validate_syntax("echo hang")

        assert result.passed is False
        assert "timed out" in (result.error_message or "").lower()

    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_os_error(self, mock_run: MagicMock) -> None:
        """OSError returns passed=False with error string."""
        mock_run.side_effect = OSError("bash not found")
        result = ReadmeValidator().validate_syntax("echo fail")

        assert result.passed is False
        assert "bash not found" in (result.error_message or "")

    @requires_bash
    def test_real_valid_syntax(self) -> None:
        """Integration: real bash validates correct syntax."""
        result = ReadmeValidator().validate_syntax("echo 'test'")
        assert result.passed is True

    @requires_bash
    def test_real_invalid_syntax(self) -> None:
        """Integration: real bash rejects bad syntax."""
        result = ReadmeValidator().validate_syntax("echo 'test")
        assert result.passed is False


# ---------------------------------------------------------------------------
# validate_availability tests (mocked)
# ---------------------------------------------------------------------------
class TestValidateAvailability:
    """Tests for ReadmeValidator.validate_availability with mocked shutil."""

    @patch("hephaestus.validation.readme_commands.shutil.which")
    def test_binary_found(self, mock_which: MagicMock) -> None:
        """Returns passed=True when binary is on PATH."""
        mock_which.return_value = "/usr/bin/echo"
        result = ReadmeValidator().validate_availability("echo test")

        assert result.passed is True
        assert result.check_type == "availability"
        assert result.error_message is None
        mock_which.assert_called_once_with("echo")

    @patch("hephaestus.validation.readme_commands.shutil.which")
    def test_binary_not_found(self, mock_which: MagicMock) -> None:
        """Returns passed=False when binary is missing."""
        mock_which.return_value = None
        result = ReadmeValidator().validate_availability("nonexistent_bin arg")

        assert result.passed is False
        assert "not found" in (result.error_message or "").lower()

    def test_empty_command(self) -> None:
        """Returns passed=False for empty command string."""
        result = ReadmeValidator().validate_availability("")

        assert result.passed is False
        assert result.check_type == "availability"
        assert "Could not extract binary" in (result.error_message or "")


# ---------------------------------------------------------------------------
# validate_execution tests (mocked)
# ---------------------------------------------------------------------------
class TestValidateExecution:
    """Tests for ReadmeValidator.validate_execution with mocked subprocess."""

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_successful_execution(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Successful command returns passed=True."""
        mock_root.return_value = Path("/repo")
        mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
        result = ReadmeValidator().validate_execution("echo hello")

        assert result.passed is True
        assert result.check_type == "execution"
        assert result.stdout == "output"
        assert result.error_message is None

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_failed_execution(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Non-zero exit returns passed=False with stderr."""
        mock_root.return_value = Path("/repo")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error msg")
        result = ReadmeValidator().validate_execution("false")

        assert result.passed is False
        assert result.check_type == "execution"
        assert result.error_message == "error msg"
        assert result.exit_code == 1

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_timeout(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """TimeoutExpired returns passed=False."""
        mock_root.return_value = Path("/repo")
        exc = subprocess.TimeoutExpired(cmd="bash", timeout=60)
        mock_run.side_effect = exc
        result = ReadmeValidator().validate_execution("sleep 999")

        assert result.passed is False
        assert "timed out" in (result.error_message or "").lower()

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_os_error(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """OSError returns passed=False."""
        mock_root.return_value = Path("/repo")
        mock_run.side_effect = OSError("no such binary")
        result = ReadmeValidator().validate_execution("nonexistent")

        assert result.passed is False
        assert "no such binary" in (result.error_message or "")

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_custom_timeout(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Custom timeout is forwarded to subprocess.run."""
        mock_root.return_value = Path("/repo")
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        ReadmeValidator().validate_execution("echo hi", timeout=30)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["timeout"] == 30


# ---------------------------------------------------------------------------
# validate_quick tests (mocked end-to-end)
# ---------------------------------------------------------------------------
class TestValidateQuick:
    """Tests for ReadmeValidator.validate_quick with mocked internals."""

    def _make_blocks(self) -> list[CodeBlock]:
        """Build a representative list of code blocks."""
        return [
            # Executable bash block with 2 safe commands
            CodeBlock(language="bash", content="echo hello\nls\n", line_number=1),
            # Non-executable python block (should be skipped)
            CodeBlock(language="python", content="print('hi')\n", line_number=10),
            # Bash block with skip marker
            CodeBlock(
                language="bash",
                content="echo skip  # SKIP-VALIDATION\n",
                line_number=20,
            ),
            # Bash block with unsafe command
            CodeBlock(language="bash", content="rm -rf /tmp/x\n", line_number=30),
        ]

    @patch("hephaestus.validation.readme_commands.shutil.which")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_quick_validation_flow(self, mock_run: MagicMock, mock_which: MagicMock) -> None:
        """Quick validation checks syntax then availability."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        mock_which.return_value = "/usr/bin/echo"

        v = ReadmeValidator()
        report = v.validate_quick(self._make_blocks())

        assert report.level == "quick"
        assert report.total_blocks == 4
        # Only the first bash block's 2 commands are counted as total
        # (the skip block adds to skipped, unsafe adds to skipped)
        assert report.total_commands >= 2
        assert report.passed >= 1
        assert report.failed == 0
        # Skip marker block + unsafe command
        assert report.skipped_commands >= 1

    @patch("hephaestus.validation.readme_commands.shutil.which")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_syntax_failure_short_circuits(
        self, mock_run: MagicMock, mock_which: MagicMock
    ) -> None:
        """When syntax fails, availability is not checked."""
        mock_run.return_value = MagicMock(returncode=2, stderr="syntax error", stdout="")
        blocks = [CodeBlock(language="bash", content="echo 'bad\n", line_number=1)]
        report = ReadmeValidator().validate_quick(blocks)

        assert report.failed == 1
        mock_which.assert_not_called()

    def test_non_executable_blocks_skipped(self) -> None:
        """Blocks with non-executable languages produce no results."""
        blocks = [
            CodeBlock(language="text", content="just text\n", line_number=1),
            CodeBlock(language="python", content="x = 1\n", line_number=5),
        ]
        report = ReadmeValidator().validate_quick(blocks)
        assert report.total_commands == 0
        assert report.results == []


# ---------------------------------------------------------------------------
# validate_comprehensive tests (mocked end-to-end)
# ---------------------------------------------------------------------------
class TestValidateComprehensive:
    """Tests for ReadmeValidator.validate_comprehensive with mocks."""

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_comprehensive_runs_execution(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Comprehensive validates via full execution."""
        mock_root.return_value = Path("/repo")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        blocks = [
            CodeBlock(language="bash", content="echo test\n", line_number=1),
        ]
        report = ReadmeValidator().validate_comprehensive(blocks)

        assert report.level == "comprehensive"
        assert report.passed == 1
        assert report.failed == 0
        assert len(report.results) == 1
        assert report.results[0].check_type == "execution"

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_comprehensive_failure(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Failed execution is recorded as failure."""
        mock_root.return_value = Path("/repo")
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="oops")
        blocks = [
            CodeBlock(language="bash", content="echo fail\n", line_number=5),
        ]
        report = ReadmeValidator().validate_comprehensive(blocks)

        assert report.failed == 1
        assert report.passed == 0

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_comprehensive_skips_unsafe(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Unsafe commands are skipped in comprehensive mode."""
        mock_root.return_value = Path("/repo")
        blocks = [
            CodeBlock(language="bash", content="rm -rf /\n", line_number=1),
        ]
        report = ReadmeValidator().validate_comprehensive(blocks)

        assert report.skipped_commands == 1
        assert report.passed == 0
        assert report.failed == 0
        mock_run.assert_not_called()

    def test_comprehensive_skips_non_executable(self) -> None:
        """Non-executable language blocks are skipped."""
        blocks = [
            CodeBlock(language="text", content="hello\n", line_number=1),
        ]
        report = ReadmeValidator().validate_comprehensive(blocks)
        assert report.total_commands == 0

    @patch("hephaestus.validation.readme_commands.get_repo_root")
    @patch("hephaestus.validation.readme_commands.subprocess.run")
    def test_comprehensive_skip_marker(self, mock_run: MagicMock, mock_root: MagicMock) -> None:
        """Blocks with skip markers are counted as skipped."""
        mock_root.return_value = Path("/repo")
        blocks = [
            CodeBlock(
                language="bash",
                content="echo hi  # SKIP-VALIDATION\n",
                line_number=1,
            ),
        ]
        report = ReadmeValidator().validate_comprehensive(blocks)

        assert report.skipped_commands == 1
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# generate_report tests
# ---------------------------------------------------------------------------
class TestGenerateReport:
    """Tests for ReadmeValidator.generate_report output format."""

    def test_all_passed_report(self, tmp_path: Path) -> None:
        """Report with no failures shows 'All Commands Passed!'."""
        report = ValidationReport(
            level="quick",
            timestamp="2024-01-01T00:00:00",
            total_blocks=2,
            total_commands=3,
            passed=3,
            failed=0,
        )
        out = tmp_path / "report.md"
        ReadmeValidator().generate_report(report, out)

        content = out.read_text()
        assert "# README.md Command Validation Results" in content
        assert "**Validation Level**: Quick" in content
        assert "**Passed**: 3" in content
        assert "**Failed**: 0" in content
        assert "All Commands Passed!" in content

    def test_report_with_failures(self, tmp_path: Path) -> None:
        """Report with failures includes failure details."""
        failed_result = ValidationResult(
            command="bad_cmd",
            passed=False,
            check_type="syntax",
            error_message="syntax error near token",
            line_number=42,
            stderr="bash: syntax error",
        )
        report = ValidationReport(
            level="comprehensive",
            timestamp="2024-06-15T12:00:00",
            total_blocks=1,
            total_commands=1,
            passed=0,
            failed=1,
            results=[failed_result],
        )
        out = tmp_path / "report.md"
        ReadmeValidator().generate_report(report, out)

        content = out.read_text()
        assert "## Failed Commands" in content
        assert "Syntax Failure (line 42)" in content
        assert "bad_cmd" in content
        assert "syntax error near token" in content
        assert "**Stderr**:" in content
        assert "bash: syntax error" in content

    def test_report_with_skipped_commands(self, tmp_path: Path) -> None:
        """Report includes skipped command count."""
        report = ValidationReport(
            level="quick",
            timestamp="2024-01-01T00:00:00",
            total_blocks=3,
            total_commands=5,
            skipped_commands=2,
            passed=3,
            failed=0,
        )
        out = tmp_path / "report.md"
        ReadmeValidator().generate_report(report, out)

        content = out.read_text()
        assert "Commands skipped: 2" in content
        assert "Commands validated: 3" in content

    def test_report_failure_no_stderr(self, tmp_path: Path) -> None:
        """Report with failure but empty stderr omits Stderr section."""
        failed_result = ValidationResult(
            command="missing_cmd",
            passed=False,
            check_type="availability",
            error_message="Binary not found: missing_cmd",
            line_number=10,
            stderr="",
        )
        report = ValidationReport(
            level="quick",
            timestamp="2024-01-01T00:00:00",
            total_blocks=1,
            total_commands=1,
            passed=0,
            failed=1,
            results=[failed_result],
        )
        out = tmp_path / "report.md"
        ReadmeValidator().generate_report(report, out)

        content = out.read_text()
        assert "## Failed Commands" in content
        assert "**Stderr**:" not in content
