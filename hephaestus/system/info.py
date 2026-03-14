#!/usr/bin/env python3
"""System information collection utilities for ProjectHephaestus.

Collects comprehensive system information for debugging, reporting, and environment analysis.
Gathers OS details, Python versions, Git information, and environment variables.
"""

import os
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hephaestus.utils.helpers import run_subprocess


def run_command(cmd: list[str], capture_output: bool = True, timeout: int = 5) -> tuple[bool, str]:
    """Run a shell command and return success status and output.

    Args:
        cmd: Command as list of strings
        capture_output: Whether to capture stdout/stderr (unused, kept for API compat)
        timeout: Timeout in seconds

    Returns:
        Tuple of (success: bool, output: str).

    """
    import subprocess as _subprocess

    try:
        result = run_subprocess(cmd, timeout=timeout, check=False)
        return (result.returncode == 0, result.stdout.strip())
    except (_subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return (False, "")


def get_command_path(cmd: str) -> str | None:
    """Get the full path of a command if it exists.

    Args:
        cmd: Command name to locate

    Returns:
        Full path to command or None if not found

    """
    success, output = run_command(["which", cmd])
    return output if success and output else None


def get_os_info() -> str:
    """Get operating system information."""
    system = platform.system()

    if system == "Linux":
        # Try to read /etc/os-release
        os_release_path = Path("/etc/os-release")
        if os_release_path.exists():
            try:
                with open(os_release_path) as f:
                    lines = f.readlines()
                    os_data = {}
                    for line in lines:
                        line = line.strip()
                        if "=" in line:
                            key, value = line.split("=", 1)
                            os_data[key] = value.strip('"')

                    name = os_data.get("NAME", "Linux")
                    version = os_data.get("VERSION", "")
                    return f"{name} {version}".strip()
            except Exception:
                pass
        return "Linux (unknown distribution)"

    elif system == "Darwin":
        # macOS
        success, version = run_command(["sw_vers", "-productVersion"])
        if success:
            return f"macOS {version}"
        return "macOS (unknown version)"

    elif system == "Windows":
        return f"Windows {platform.release()}"

    else:
        return f"{system} (unknown)"


def get_tool_info(
    tool_name: str,
    version_flag: str = "--version",
    version_extract: Callable[[str], str] | None = None,
) -> tuple[str, str]:
    """Get version and path information for a tool.

    Args:
        tool_name: Name of the tool
        version_flag: Flag to get version (default: --version)
        version_extract: Optional function to extract version from output

    Returns:
        Tuple of (version: str, path: str).

    """
    path = get_command_path(tool_name)

    if not path:
        return ("Not found", "")

    success, output = run_command([tool_name, version_flag])

    if not success or not output:
        version = "Unable to determine"
    elif version_extract:
        version = version_extract(output)
    else:
        version = output

    return (version, path)


def extract_version_word(output: str, word_index: int = 1) -> str:
    """Extract version by splitting output and taking word at index."""
    parts = output.split()
    return parts[word_index] if len(parts) > word_index else output


def get_python_info() -> dict[str, Any]:
    """Get comprehensive Python information."""
    return {
        "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "version_full": sys.version,
        "path": sys.executable,
        "implementation": platform.python_implementation(),
        "compiler": platform.python_compiler(),
    }


def get_git_info() -> dict[str, str]:
    """Get Git repository information if in a Git repo."""
    git_info = {
        "repository": "No",
        "branch": "",
        "commit": "",
    }

    # Check if in a git repository
    success, _ = run_command(["git", "rev-parse", "--git-dir"])
    if success:
        git_info["repository"] = "Yes"

        # Get current branch
        branch_success, branch = run_command(["git", "branch", "--show-current"])
        if branch_success and branch:
            git_info["branch"] = branch

        # Get current commit
        commit_success, commit = run_command(["git", "rev-parse", "--short", "HEAD"])
        if commit_success and commit:
            git_info["commit"] = commit

    return git_info


def get_environment_info() -> dict[str, str]:
    """Get selected environment variables."""
    env_vars = ["SHELL", "LANG", "USER", "HOME", "PATH"]
    return {var: os.environ.get(var, "Not set") for var in env_vars}


def get_system_info(include_tools: bool = True) -> dict[str, Any]:
    """Collect comprehensive system information.

    Args:
        include_tools: Whether to include tool versions (may be slow)

    Returns:
        Dictionary containing system information organized by category

    """
    info = {
        "os": {
            "name": get_os_info(),
            "kernel": platform.system(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "python": get_python_info(),
        "directory": {
            "current": str(Path.cwd()),
        },
        "git": get_git_info(),
        "environment": get_environment_info(),
    }

    if include_tools:
        # Common development tools
        tools = {
            "git": get_tool_info("git", version_extract=lambda x: extract_version_word(x, 2)),
            "pip": get_tool_info("pip"),
            "docker": get_tool_info("docker"),
            "node": get_tool_info("node"),
            "npm": get_tool_info("npm"),
        }
        info["tools"] = tools

    return info


def format_system_info(info: dict[str, Any], format_type: str = "text") -> str:
    """Format system information for display.

    Args:
        info: System information dictionary
        format_type: Output format ("text" or "json")

    Returns:
        Formatted string representation

    """
    if format_type.lower() == "json":
        import json

        return json.dumps(info, indent=2)

    # Text format
    output = []
    output.append("=== System Information ===")
    output.append("")

    # OS Information
    output.append("OS Information:")
    output.append(f"  OS: {info['os']['name']}")
    output.append(f"  Kernel: {info['os']['kernel']}")
    output.append(f"  Machine: {info['os']['machine']}")
    output.append(f"  Processor: {info['os']['processor']}")
    output.append("")

    # Python Information
    output.append("Python:")
    python_info = info["python"]
    output.append(f"  Version: {python_info['version']}")
    output.append(f"  Implementation: {python_info['implementation']}")
    output.append(f"  Compiler: {python_info['compiler']}")
    output.append(f"  Path: {python_info['path']}")
    output.append("")

    # Git Information
    output.append("Git:")
    git_info = info["git"]
    output.append(f"  Repository: {git_info['repository']}")
    if git_info["branch"]:
        output.append(f"  Branch: {git_info['branch']}")
    if git_info["commit"]:
        output.append(f"  Commit: {git_info['commit']}")
    output.append("")

    # Directory Information
    output.append("Directory:")
    output.append(f"  Current: {info['directory']['current']}")
    output.append("")

    # Environment Information
    output.append("Environment:")
    for key, value in info["environment"].items():
        output.append(f"  {key}: {value}")
    output.append("")

    # Tools Information (if available)
    if "tools" in info:
        output.append("Tools:")
        for tool_name, (version, path) in info["tools"].items():
            if path:
                output.append(f"  {tool_name}: {version}")
            else:
                output.append(f"  {tool_name}: Not found")
        output.append("")

    output.append("=== End System Information ===")
    return "\n".join(output)


def main():
    """Main function to collect and display system information."""
    import argparse

    parser = argparse.ArgumentParser(description="Collect system information")
    parser.add_argument("--json", action="store_true", help="Output in JSON format")
    parser.add_argument("--no-tools", action="store_true", help="Skip tool version checks")

    args = parser.parse_args()

    info = get_system_info(include_tools=not args.no_tools)
    format_type = "json" if args.json else "text"
    print(format_system_info(info, format_type))


if __name__ == "__main__":
    main()
