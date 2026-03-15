#!/usr/bin/env python3

"""README Command Validation.

Extracts and validates commands from README.md code blocks to ensure
documented commands actually work.

Validation Levels:
    quick:         Syntax check and binary availability (nightly)
    comprehensive: Full command execution with timeout (weekly)
"""

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from hephaestus.utils.helpers import get_repo_root

# Command classification by language tag
EXECUTE_LANGUAGES = {"bash", "shell", "sh"}
SKIP_LANGUAGES = {"text", "plaintext", "output", "console", "markdown", ""}
SYNTAX_CHECK_LANGUAGES = {"python"}

# Skip markers - commands with these comments are not executed
SKIP_MARKERS = ["# SKIP-VALIDATION", "# OPTIONAL", "# EXAMPLE"]

# Safety: blocked patterns (never execute)
BLOCKED_PATTERNS = [
    r"\brm\s",
    r"\bmv\s",
    r"\bcp\s",
    r">",
    r">>",
    r"\bgit\s+(commit|push|checkout|reset)",
    r"\bsudo\b",
    r"\bpip\s+install",
    r"\bnpm\s+install",
    r"\bcurl\s+.*\|\s*(bash|sh)",  # Pipe to shell
]


@dataclass
class CodeBlock:
    """Represents a fenced code block from markdown."""

    language: str
    content: str
    line_number: int

    def commands(self) -> list[str]:
        """Extract individual commands from the code block."""
        lines = []
        for line in self.content.strip().split("\n"):
            line = line.strip()
            # Skip empty lines and comments
            if not line or line.startswith("#"):
                continue
            # Skip continuation lines (handled with previous)
            if line.startswith("\\"):
                continue
            lines.append(line)
        return lines

    def has_skip_marker(self) -> bool:
        """Check if block contains a skip marker."""
        return any(marker in self.content for marker in SKIP_MARKERS)


@dataclass
class ValidationResult:
    """Result of validating a command."""

    command: str
    passed: bool
    check_type: str  # "syntax", "availability", "execution"
    error_message: str | None = None
    line_number: int = 0
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0


@dataclass
class ValidationReport:
    """Full validation report."""

    level: str
    timestamp: str
    total_blocks: int = 0
    total_commands: int = 0
    skipped_commands: int = 0
    passed: int = 0
    failed: int = 0
    results: list[ValidationResult] = field(default_factory=list)


class ReadmeValidator:
    """Validates commands in README markdown files."""

    # Default allowed command prefixes (can be overridden)
    DEFAULT_ALLOWED_PREFIXES: ClassVar[list[str]] = [
        "pixi run",
        "pixi install",
        "pixi info",
        "just precommit",
        "python3 -m py_compile",
        "python3 --version",
        "gh auth status",
        "gh issue list",
        "gh issue view",
        "gh pr list",
        "gh pr view",
        "echo",
        "cat",
        "ls",
        "pwd",
        "which",
    ]

    def __init__(self, allowed_prefixes: list[str] | None = None):
        """Initialize the readme validator.

        Args:
            allowed_prefixes: Custom list of allowed command prefixes. If None, uses defaults.

        """
        self.allowed_prefixes = allowed_prefixes or self.DEFAULT_ALLOWED_PREFIXES

    def extract_code_blocks(self, markdown_path: Path) -> list[CodeBlock]:
        """Extract fenced code blocks from markdown file.

        Args:
            markdown_path: Path to markdown file

        Returns:
            List of CodeBlock objects

        """
        content = markdown_path.read_text()
        blocks = []

        # Match fenced code blocks: ```language\n...\n```
        pattern = r"^```(\w*)\n(.*?)^```"
        matches = re.finditer(pattern, content, re.MULTILINE | re.DOTALL)

        for match in matches:
            language = match.group(1).lower()
            block_content = match.group(2)

            # Calculate line number
            line_number = content[: match.start()].count("\n") + 1

            blocks.append(
                CodeBlock(language=language, content=block_content, line_number=line_number)
            )

        return blocks

    def is_blocked_command(self, command: str) -> bool:
        """Check if command matches blocked patterns.

        Args:
            command: Command string to check

        Returns:
            True if command is blocked

        """
        return any(re.search(pattern, command) for pattern in BLOCKED_PATTERNS)

    def is_allowed_command(self, command: str) -> bool:
        """Check if command starts with an allowed prefix.

        Args:
            command: Command string to check

        Returns:
            True if command is allowed

        """
        return any(command.startswith(prefix) for prefix in self.allowed_prefixes)

    def is_safe_command(self, command: str) -> tuple[bool, str]:
        """Check if command is safe to execute.

        Args:
            command: Command string to check

        Returns:
            Tuple of (is_safe, reason).

        """
        if self.is_blocked_command(command):
            return False, "matches blocked pattern"

        if not self.is_allowed_command(command):
            return False, "not in allowed prefixes"

        return True, "allowed"

    def get_binary_from_command(self, command: str) -> str:
        """Extract the binary/executable from a command.

        Args:
            command: Command string

        Returns:
            Binary name

        """
        parts = command.split()
        if not parts:
            return ""
        return parts[0]

    def validate_syntax(self, command: str) -> ValidationResult:
        """Validate bash syntax of a command.

        Args:
            command: Command string to validate

        Returns:
            ValidationResult

        """
        try:
            result = subprocess.run(
                ["bash", "-n", "-c", command],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return ValidationResult(
                command=command,
                passed=result.returncode == 0,
                check_type="syntax",
                error_message=result.stderr if result.returncode != 0 else None,
                exit_code=result.returncode,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(
                command=command,
                passed=False,
                check_type="syntax",
                error_message="Syntax check timed out",
            )
        except (OSError, ValueError) as e:
            return ValidationResult(
                command=command,
                passed=False,
                check_type="syntax",
                error_message=str(e),
            )

    def validate_availability(self, command: str) -> ValidationResult:
        """Check if command binary is available.

        Args:
            command: Command string to check

        Returns:
            ValidationResult

        """
        binary = self.get_binary_from_command(command)
        if not binary:
            return ValidationResult(
                command=command,
                passed=False,
                check_type="availability",
                error_message="Could not extract binary from command",
            )

        found = shutil.which(binary) is not None
        return ValidationResult(
            command=command,
            passed=found,
            check_type="availability",
            error_message=f"Binary not found: {binary}" if not found else None,
        )

    def validate_execution(self, command: str, timeout: int = 60) -> ValidationResult:
        """Execute command and validate it succeeds.

        Args:
            command: Command string to execute
            timeout: Timeout in seconds

        Returns:
            ValidationResult

        """
        try:
            result = subprocess.run(
                ["bash", "-c", command],
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=get_repo_root(),  # Run from repo root
            )
            return ValidationResult(
                command=command,
                passed=result.returncode == 0,
                check_type="execution",
                error_message=result.stderr if result.returncode != 0 else None,
                exit_code=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        except subprocess.TimeoutExpired:
            return ValidationResult(
                command=command,
                passed=False,
                check_type="execution",
                error_message=f"Command timed out after {timeout}s",
            )
        except (OSError, ValueError) as e:
            return ValidationResult(
                command=command,
                passed=False,
                check_type="execution",
                error_message=str(e),
            )

    def validate_quick(self, blocks: list[CodeBlock]) -> ValidationReport:
        """Quick validation: syntax and availability checks.

        Args:
            blocks: List of code blocks to validate

        Returns:
            ValidationReport

        """
        report = ValidationReport(
            level="quick",
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_blocks=len(blocks),
        )

        for block in blocks:
            # Skip non-executable blocks
            if block.language not in EXECUTE_LANGUAGES:
                continue

            # Skip blocks with skip markers
            if block.has_skip_marker():
                report.skipped_commands += len(block.commands())
                continue

            for command in block.commands():
                report.total_commands += 1

                # Check safety
                is_safe, _reason = self.is_safe_command(command)
                if not is_safe:
                    report.skipped_commands += 1
                    continue

                # Syntax check
                syntax_result = self.validate_syntax(command)
                syntax_result.line_number = block.line_number
                report.results.append(syntax_result)

                if syntax_result.passed:
                    # Availability check
                    avail_result = self.validate_availability(command)
                    avail_result.line_number = block.line_number
                    report.results.append(avail_result)

                    if avail_result.passed:
                        report.passed += 1
                    else:
                        report.failed += 1
                else:
                    report.failed += 1

        return report

    def validate_comprehensive(self, blocks: list[CodeBlock]) -> ValidationReport:
        """Comprehensive validation: full command execution.

        Args:
            blocks: List of code blocks to validate

        Returns:
            ValidationReport

        """
        report = ValidationReport(
            level="comprehensive",
            timestamp=datetime.now(timezone.utc).isoformat(),
            total_blocks=len(blocks),
        )

        for block in blocks:
            # Skip non-executable blocks
            if block.language not in EXECUTE_LANGUAGES:
                continue

            # Skip blocks with skip markers
            if block.has_skip_marker():
                report.skipped_commands += len(block.commands())
                continue

            for command in block.commands():
                report.total_commands += 1

                # Check safety
                is_safe, _reason = self.is_safe_command(command)
                if not is_safe:
                    report.skipped_commands += 1
                    continue

                # Full execution
                exec_result = self.validate_execution(command)
                exec_result.line_number = block.line_number
                report.results.append(exec_result)

                if exec_result.passed:
                    report.passed += 1
                else:
                    report.failed += 1

        return report

    def generate_report(self, report: ValidationReport, output_path: Path) -> None:
        """Generate markdown validation report.

        Args:
            report: ValidationReport to format
            output_path: Path to write report

        """
        lines = [
            "# README.md Command Validation Results",
            "",
            f"**Validation Level**: {report.level.title()}",
            f"**Timestamp**: {report.timestamp} UTC",
            "",
            "## Summary",
            "",
            f"- Total code blocks: {report.total_blocks}",
            f"- Total commands found: {report.total_commands}",
            f"- Commands validated: {report.passed + report.failed}",
            f"- Commands skipped: {report.skipped_commands}",
            f"- **Passed**: {report.passed}",
            f"- **Failed**: {report.failed}",
            "",
        ]

        # Failed commands section
        failed = [r for r in report.results if not r.passed]
        if failed:
            lines.extend(["## Failed Commands", ""])
            for i, result in enumerate(failed, 1):
                lines.extend(
                    [
                        f"### {i}. {result.check_type.title()} Failure (line {result.line_number})",
                        "",
                        "```bash",
                        result.command,
                        "```",
                        "",
                        f"**Error**: {result.error_message}",
                        "",
                    ]
                )
                if result.stderr:
                    lines.extend(
                        [
                            "**Stderr**:",
                            "```",
                            result.stderr[:500],  # Truncate long output
                            "```",
                            "",
                        ]
                    )
        else:
            lines.extend(["## All Commands Passed!", ""])

        # Write report
        output_path.write_text("\n".join(lines))
