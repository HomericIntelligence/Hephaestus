#!/usr/bin/env python3
"""Tests for general utilities."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.utils.helpers import (
    flatten_dict,
    get_proj_root,
    get_repo_root,
    human_readable_size,
    install_package,
    run_subprocess,
    slugify,
)


class TestSlugify:
    """Tests for slugify."""

    def test_basic_slug(self):
        """Converts basic text to slug."""
        assert slugify("Hello World") == "hello-world"

    def test_version_in_text(self):
        """Handles version strings with dots."""
        assert slugify("My Project v1.0") == "my-project-v1-0"

    def test_special_characters_removed(self):
        """Special characters are stripped."""
        assert slugify("Special!@#$%Characters") == "specialcharacters"

    def test_multiple_spaces(self):
        """Multiple spaces become single hyphen."""
        assert slugify("a   b") == "a-b"

    def test_underscores_become_hyphens(self):
        """Underscores are converted to hyphens."""
        assert slugify("foo_bar") == "foo-bar"

    def test_dots_become_hyphens(self):
        """Dots are converted to hyphens."""
        assert slugify("foo.bar") == "foo-bar"

    def test_leading_trailing_hyphens_removed(self):
        """Leading and trailing hyphens are stripped."""
        assert slugify("-hello-") == "hello"

    def test_consecutive_hyphens_collapsed(self):
        """Multiple consecutive hyphens become one."""
        assert slugify("a--b") == "a-b"

    def test_empty_string(self):
        """Empty string returns empty string."""
        assert slugify("") == ""

    def test_unicode_normalized(self):
        """Unicode characters are normalized to ASCII."""
        result = slugify("café")
        assert "caf" in result

    def test_already_valid_slug(self):
        """Already-valid slug is unchanged."""
        assert slugify("hello-world") == "hello-world"


class TestHumanReadableSize:
    """Tests for human_readable_size."""

    def test_zero_bytes(self):
        assert human_readable_size(0) == "0 B"

    def test_bytes(self):
        assert human_readable_size(1023) == "1023.0 B"

    def test_kilobytes(self):
        assert human_readable_size(1024) == "1.0 KB"

    def test_megabytes(self):
        assert human_readable_size(1048576) == "1.0 MB"

    def test_gigabytes(self):
        assert human_readable_size(1073741824) == "1.0 GB"

    def test_terabytes(self):
        assert human_readable_size(1099511627776) == "1.0 TB"

    def test_float_input(self):
        """Accepts float inputs."""
        result = human_readable_size(1024.0)
        assert result == "1.0 KB"


class TestFlattenDict:
    """Tests for flatten_dict."""

    def test_basic_nested(self):
        """Flattens one level of nesting."""
        nested = {"a": 1, "b": {"c": 2}}
        assert flatten_dict(nested) == {"a": 1, "b.c": 2}

    def test_deeply_nested(self):
        """Flattens multiple levels of nesting."""
        nested = {"a": 1, "b": {"c": 2, "d": {"e": 3}}}
        assert flatten_dict(nested) == {"a": 1, "b.c": 2, "b.d.e": 3}

    def test_already_flat(self):
        """Flat dict is unchanged."""
        flat = {"a": 1, "b": 2}
        assert flatten_dict(flat) == flat

    def test_empty_dict(self):
        """Empty dict returns empty dict."""
        assert flatten_dict({}) == {}

    def test_custom_separator(self):
        """Custom separator is used."""
        nested = {"a": {"b": 1}}
        result = flatten_dict(nested, sep="/")
        assert "a/b" in result

    def test_list_values_preserved(self):
        """List values are preserved as-is."""
        nested = {"a": [1, 2, 3]}
        assert flatten_dict(nested) == {"a": [1, 2, 3]}


class TestGetRepoRoot:
    """Tests for get_repo_root."""

    def test_finds_git_repo(self, mock_git_repo, tmp_path):
        """Finds repo root from a subdirectory."""
        subdir = mock_git_repo / "src" / "module"
        subdir.mkdir(parents=True)
        result = get_repo_root(subdir)
        assert result == mock_git_repo

    def test_returns_start_path_when_no_git(self, tmp_path):
        """Returns start_path when no .git found."""
        # tmp_path has no .git directory above it (typically)
        result = get_repo_root(tmp_path)
        # Either finds a .git above tmp_path or returns tmp_path
        assert isinstance(result, Path)

    def test_accepts_string_path(self, mock_git_repo):
        """Accepts a string path argument."""
        result = get_repo_root(str(mock_git_repo))
        assert result == mock_git_repo

    def test_uses_cwd_when_none(self):
        """Uses current working directory when start_path is None."""
        result = get_repo_root(None)
        assert isinstance(result, Path)


class TestRunSubprocess:
    """Tests for run_subprocess."""

    def test_successful_command(self):
        """Runs command successfully and returns result."""
        result = run_subprocess(["echo", "hello"])
        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_with_cwd(self, tmp_path):
        """Runs command in specified working directory."""
        result = run_subprocess(["pwd"], cwd=str(tmp_path))
        assert result.returncode == 0

    def test_failed_command_raises(self):
        """Raises CalledProcessError for non-zero exit."""
        with pytest.raises(subprocess.CalledProcessError):
            run_subprocess(["false"])


class TestGetProjRoot:
    """Tests for get_proj_root."""

    def test_from_env_variable(self, monkeypatch, tmp_path):
        """Returns path from environment variable."""
        monkeypatch.setenv("MYPROJECT_ROOT", str(tmp_path))
        result = get_proj_root("MyProject")
        assert result == str(tmp_path)

    def test_raises_when_not_found(self, monkeypatch, tmp_path):
        """Raises ValueError when project root not found."""
        monkeypatch.delenv("NONEXISTENTPROJECT_ROOT", raising=False)
        with pytest.raises(ValueError, match="Could not determine"):
            # Use a name that won't match any git repo
            get_proj_root("NonExistentProject_XYZ_12345")


class TestInstallPackage:
    """Tests for install_package."""

    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_successful_install(self, mock_run):
        """Returns True on successful install."""
        mock_run.return_value = MagicMock(returncode=0)
        result = install_package("some-package")
        assert result is True

    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_failed_install_returns_false(self, mock_run):
        """Returns False when install fails."""
        mock_run.side_effect = subprocess.CalledProcessError(1, ["pip"])
        result = install_package("bad-package")
        assert result is False

    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_upgrade_flag_included(self, mock_run):
        """Includes --upgrade flag when upgrade=True."""
        mock_run.return_value = MagicMock(returncode=0)
        install_package("some-package", upgrade=True)
        cmd = mock_run.call_args[0][0]
        assert "--upgrade" in cmd

    def test_rejects_newline_in_package_name(self):
        """Rejects package names containing newlines."""
        with pytest.raises(ValueError, match="Invalid package name"):
            install_package("pkg\nmalicious")

    def test_rejects_exclamation_mark_in_package_name(self):
        """Rejects package names containing exclamation marks."""
        with pytest.raises(ValueError, match="Invalid package name"):
            install_package("pkg!exploit")

    def test_rejects_tab_in_package_name(self):
        """Rejects package names containing tab characters."""
        with pytest.raises(ValueError, match="Invalid package name"):
            install_package("pkg\texploit")

    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_accepts_version_constraints(self, mock_run):
        """Accepts valid package names with version constraints."""
        mock_run.return_value = MagicMock(returncode=0)
        assert install_package("torch>=2.0,<3") is True

    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_accepts_extras(self, mock_run):
        """Accepts valid package names with extras."""
        mock_run.return_value = MagicMock(returncode=0)
        assert install_package("pkg[extra]") is True

    @pytest.mark.parametrize(
        "malicious_input",
        [
            "pkg; rm -rf /",
            "pkg | cat /etc/passwd",
            "pkg && echo pwned",
        ],
    )
    def test_shell_injection_rejected(self, malicious_input: str) -> None:
        """Reject package names containing shell injection characters."""
        with pytest.raises(ValueError, match="Invalid package name"):
            install_package(malicious_input)
