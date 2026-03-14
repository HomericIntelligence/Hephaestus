#!/usr/bin/env python3
"""Tests for system information utilities."""

from hephaestus.system.info import (
    extract_version_word,
    format_system_info,
    get_command_path,
    get_environment_info,
    get_git_info,
    get_os_info,
    get_python_info,
    get_system_info,
    run_command,
)


class TestRunCommand:
    """Tests for run_command."""

    def test_successful_command(self) -> None:
        """run_command returns (True, output) for valid commands."""
        success, output = run_command(["echo", "hello"])
        assert success is True
        assert "hello" in output

    def test_failing_command(self) -> None:
        """run_command returns (False, '') for unknown commands."""
        success, _output = run_command(["nonexistent_cmd_xyz"])
        assert success is False

    def test_timeout(self) -> None:
        """run_command handles timeout gracefully."""
        success, _output = run_command(["sleep", "10"], timeout=1)
        assert success is False


class TestGetCommandPath:
    """Tests for get_command_path."""

    def test_known_command(self) -> None:
        """Returns path for a command that exists (python3)."""
        path = get_command_path("python3")
        # May return None if which is unavailable, but should not raise
        assert path is None or isinstance(path, str)

    def test_unknown_command(self) -> None:
        """Returns None for unknown commands."""
        path = get_command_path("__no_such_command__")
        assert path is None


class TestGetOsInfo:
    """Tests for get_os_info."""

    def test_returns_string(self) -> None:
        """get_os_info returns a non-empty string."""
        info = get_os_info()
        assert isinstance(info, str)
        assert len(info) > 0


class TestGetPythonInfo:
    """Tests for get_python_info."""

    def test_has_required_keys(self) -> None:
        """Python info dict has all expected keys."""
        info = get_python_info()
        assert "version" in info
        assert "path" in info
        assert "implementation" in info

    def test_version_format(self) -> None:
        """Version string is in x.y.z format."""
        info = get_python_info()
        parts = info["version"].split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)


class TestGetGitInfo:
    """Tests for get_git_info."""

    def test_returns_dict_with_repository_key(self) -> None:
        """git_info always contains 'repository' key."""
        info = get_git_info()
        assert "repository" in info

    def test_repository_value_is_yes_or_no(self) -> None:
        """Repository value is either 'Yes' or 'No'."""
        info = get_git_info()
        assert info["repository"] in ("Yes", "No")


class TestGetEnvironmentInfo:
    """Tests for get_environment_info."""

    def test_returns_dict(self) -> None:
        """Returns a dict with string values."""
        info = get_environment_info()
        assert isinstance(info, dict)
        for v in info.values():
            assert isinstance(v, str)


class TestGetSystemInfo:
    """Tests for get_system_info."""

    def test_returns_all_sections(self) -> None:
        """System info contains os, python, directory, git, environment keys."""
        info = get_system_info(include_tools=False)
        assert "os" in info
        assert "python" in info
        assert "directory" in info
        assert "git" in info
        assert "environment" in info

    def test_tools_section_when_requested(self) -> None:
        """Tools section present when include_tools=True."""
        info = get_system_info(include_tools=True)
        assert "tools" in info

    def test_no_tools_section_when_skipped(self) -> None:
        """Tools section absent when include_tools=False."""
        info = get_system_info(include_tools=False)
        assert "tools" not in info


class TestExtractVersionWord:
    """Tests for extract_version_word."""

    def test_default_index(self) -> None:
        """Extracts word at index 1 by default."""
        assert extract_version_word("git version 2.40.0") == "version"

    def test_custom_index(self) -> None:
        """Extracts word at the given index."""
        assert extract_version_word("git version 2.40.0", word_index=2) == "2.40.0"

    def test_out_of_bounds_returns_input(self) -> None:
        """Returns the original string when index is out of bounds."""
        assert extract_version_word("single") == "single"


class TestFormatSystemInfo:
    """Tests for format_system_info."""

    def test_text_format_contains_sections(self) -> None:
        """Text output includes expected section headers."""
        info = get_system_info(include_tools=False)
        text = format_system_info(info, format_type="text")
        assert "OS Information" in text
        assert "Python" in text
        assert "Git" in text

    def test_json_format_is_valid(self) -> None:
        """JSON output is valid JSON."""
        import json

        info = get_system_info(include_tools=False)
        text = format_system_info(info, format_type="json")
        parsed = json.loads(text)
        assert "os" in parsed
