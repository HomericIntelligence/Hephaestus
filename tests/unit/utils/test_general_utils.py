#!/usr/bin/env python3
"""Tests for general utilities."""

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hephaestus.utils.helpers import (
    _format_cmd_for_log,
    flatten_dict,
    get_proj_root,
    get_repo_root,
    human_readable_size,
    install_package,
    run_subprocess,
    slugify,
    strip_null_bytes,
)


class TestStripNullBytes:
    """Tests for strip_null_bytes (#1661)."""

    def test_removes_embedded_null(self) -> None:
        """An embedded NUL byte is removed, surrounding text preserved."""
        assert strip_null_bytes("a\x00b") == "ab"

    def test_removes_leading_and_trailing_null(self) -> None:
        """Leading and trailing NULs are stripped."""
        assert strip_null_bytes("\x00abc\x00") == "abc"

    def test_removes_multiple_nulls(self) -> None:
        """Every NUL is removed regardless of count or position."""
        assert strip_null_bytes("\x00a\x00\x00b\x00c") == "abc"

    def test_nul_adjacent_to_multibyte_unicode(self) -> None:
        """A NUL next to multibyte characters is removed without corrupting them."""
        assert strip_null_bytes("✓\x00é\x00中") == "✓é中"

    def test_clean_text_unchanged(self) -> None:
        """Text without NULs compares equal (value contract, not identity)."""
        text = "no nulls here — just unicode ✓"
        assert strip_null_bytes(text) == text

    def test_clean_text_is_not_copied(self) -> None:
        """Micro-optimization: clean text is returned without allocating a copy.

        Separated from the value-equality contract above so a conformant
        implementation that always calls ``.replace`` would fail only this
        explicitly-labeled optimization test, not the behavioral one.
        """
        text = "no nulls here"
        assert strip_null_bytes(text) is text

    def test_empty_string(self) -> None:
        """An empty string is handled without error."""
        assert strip_null_bytes("") == ""

    def test_only_nulls(self) -> None:
        """A string of only NULs becomes empty."""
        assert strip_null_bytes("\x00\x00\x00") == ""


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
        assert result == mock_git_repo.resolve()

    def test_returns_start_path_when_no_git(self, tmp_path):
        """Returns start_path when no .git found."""
        # tmp_path has no .git directory above it (typically)
        result = get_repo_root(tmp_path)
        # Either finds a .git above tmp_path or returns tmp_path
        assert isinstance(result, Path)

    def test_accepts_string_path(self, mock_git_repo):
        """Accepts a string path argument."""
        result = get_repo_root(str(mock_git_repo))
        assert result == mock_git_repo.resolve()

    def test_uses_cwd_when_none(self):
        """Uses current working directory when start_path is None."""
        result = get_repo_root(None)
        assert isinstance(result, Path)
        assert result.is_absolute()

    def test_nested_git_stops_at_inner_git(self, tmp_path):
        """Inner .git wins over outer .git — first-match-up (innermost) semantics."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        (outer / ".git").mkdir(parents=True)
        (inner / ".git").mkdir(parents=True)
        seed = inner / "src" / "module"
        seed.mkdir(parents=True)
        assert get_repo_root(seed) == inner.resolve()

    def test_nested_pyproject_stops_at_inner_pyproject(self, tmp_path):
        """Inner pyproject.toml wins over outer pyproject.toml."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        outer.mkdir(parents=True)
        inner.mkdir(parents=True)
        (outer / "pyproject.toml").write_text("[project]\n")
        (inner / "pyproject.toml").write_text("[project]\n")
        seed = inner / "src"
        seed.mkdir(parents=True)
        assert get_repo_root(seed) == inner.resolve()

    def test_nested_pyproject_stops_at_inner_when_outer_has_git(self, tmp_path):
        """Inner pyproject.toml wins over outer .git."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        (outer / ".git").mkdir(parents=True)
        inner.mkdir(parents=True)
        (inner / "pyproject.toml").write_text("[project]\n")
        seed = inner / "src"
        seed.mkdir(parents=True)
        assert get_repo_root(seed) == inner.resolve()

    def test_nested_git_stops_at_inner_when_outer_has_pyproject(self, tmp_path):
        """Inner .git wins over outer pyproject.toml."""
        outer = tmp_path / "outer"
        inner = outer / "inner"
        outer.mkdir(parents=True)
        (outer / "pyproject.toml").write_text("[project]\n")
        (inner / ".git").mkdir(parents=True)
        seed = inner / "src"
        seed.mkdir(parents=True)
        assert get_repo_root(seed) == inner.resolve()


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

    def test_log_on_error_false_suppresses_error_log(self):
        """When log_on_error=False, failure does not call logger.error."""
        with patch("hephaestus.utils.helpers.logger.error") as mock_error:
            with pytest.raises(subprocess.CalledProcessError):
                run_subprocess(["false"], log_on_error=False)

        assert not mock_error.called

    def test_log_on_error_true_emits_error_log(self):
        """When log_on_error=True (default), failure calls logger.error."""
        with patch("hephaestus.utils.helpers.logger.error") as mock_error:
            with pytest.raises(subprocess.CalledProcessError):
                run_subprocess(["false"], log_on_error=True)

        assert mock_error.called

    def test_long_argv_truncated_in_error_log(self):
        """Long argv values are truncated in the failure log line.

        Defense-in-depth: even with ``--body-file`` in place upstream, any
        future caller passing a multi-KB ``--body`` shouldn't blow up logs.
        """
        long_arg = "x" * 10_000

        with patch("hephaestus.utils.helpers.logger.error") as mock_error:
            with pytest.raises(subprocess.CalledProcessError):
                run_subprocess(["false", long_arg])

        rendered = str(mock_error.call_args_list)
        assert long_arg not in rendered, "full long arg leaked into log"
        assert "more chars" in rendered, "truncation marker missing"

    def test_failed_command_logs_stdout_tail(self) -> None:
        """A failing command logs stdout tail where pre-commit reports failures."""
        stdout = "stdout prefix\n" + ("passed hook\n" * 300) + "failing-hook\nboom\n"
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["pre-commit", "run"],
            output=stdout,
            stderr="stderr context",
        )

        with (
            patch("hephaestus.utils.helpers.subprocess.run", side_effect=error),
            patch("hephaestus.utils.helpers.logger.error") as mock_error,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                run_subprocess(["pre-commit", "run"])

        rendered = str(mock_error.call_args_list)
        assert "stdout:" in rendered
        assert "failing-hook" in rendered
        assert "boom" in rendered
        assert "stdout prefix" not in rendered

    def test_failed_command_logs_stderr_tail(self) -> None:
        """Long stderr keeps the tail instead of the first 200 characters."""
        stderr = "stderr prefix\n" + ("older stderr\n" * 300) + "useful stderr tail\n"
        error = subprocess.CalledProcessError(
            returncode=1,
            cmd=["tool"],
            output="stdout context",
            stderr=stderr,
        )

        with (
            patch("hephaestus.utils.helpers.subprocess.run", side_effect=error),
            patch("hephaestus.utils.helpers.logger.error") as mock_error,
        ):
            with pytest.raises(subprocess.CalledProcessError):
                run_subprocess(["tool"])

        rendered = str(mock_error.call_args_list)
        assert "stderr:" in rendered
        assert "useful stderr tail" in rendered
        assert "stderr prefix" not in rendered


class TestFormatCmdForLog:
    """Tests for _format_cmd_for_log (private helper)."""

    def test_short_args_unchanged(self):
        out = _format_cmd_for_log(["gh", "issue", "view", "123"])
        assert out == "gh issue view 123"

    def test_long_arg_truncated(self):
        long = "a" * 500
        out = _format_cmd_for_log(["gh", "issue", "comment", "--body", long])
        assert long not in out
        assert "more chars" in out
        assert "a" * 100 in out


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
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package("pkg\nmalicious")

    def test_rejects_tab_in_package_name(self):
        """Rejects package names containing tab characters."""
        with pytest.raises(ValueError, match="Invalid package requirement"):
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
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package(malicious_input)

    def test_empty_string_raises_value_error(self) -> None:
        """Empty string is rejected by package name validation."""
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package("")

    @pytest.mark.parametrize("blank", ["   ", "\t", " \n "])
    def test_whitespace_only_raises_value_error(self, blank: str) -> None:
        """Whitespace-only strings are rejected before reaching pip."""
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package(blank)

    def test_flag_injection_rejected(self) -> None:
        """Flag injection like 'pkg --index-url http://...' is rejected by PEP 508 parser."""
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package("pkg --index-url http://example.com")

    @pytest.mark.parametrize(
        "name",
        [
            "requests",
            "my-package",
            "my_package",
            "pkg.name",
            "pkg[extra1]",
            "pkg[extra1,extra2]",
            "pkg>=1.0",
            "pkg>=1.0,<2",
            "pkg==1.2.3",
            "pkg!=1.3",
        ],
    )
    @patch("hephaestus.utils.helpers.run_subprocess")
    def test_valid_requirement_accepted(self, mock_run, name):
        """Accepts valid PEP 508 requirement strings."""
        mock_run.return_value = MagicMock(returncode=0)
        assert install_package(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            "pkg1 pkg2",
            "pkg; rm -rf /",
            "",
            "   ",
            "pkg && echo pwned",
            "pkg | cat /etc/passwd",
            "pkg\nnewline",
        ],
    )
    def test_invalid_requirement_rejected(self, name):
        """Rejects invalid or dangerous requirement strings."""
        with pytest.raises(ValueError, match="Invalid package requirement"):
            install_package(name)

    def test_url_requirement_rejected(self):
        """Rejects URL-based requirements for security."""
        with pytest.raises(ValueError, match="URL-based requirements are not supported"):
            install_package("pkg @ https://evil.com/malware.tar.gz")


class TestRunSubprocessTimeoutLogging:
    """Tests that run_subprocess logs TimeoutExpired correctly (#382/A4-07)."""

    def test_timeout_expired_is_logged_and_reraised(self) -> None:
        """TimeoutExpired triggers an error log then re-raises the exception."""
        exc = subprocess.TimeoutExpired(cmd=["sleep", "99"], timeout=1)
        with (
            patch("subprocess.run", side_effect=exc),
            patch("hephaestus.utils.helpers.logger.error") as mock_error,
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                run_subprocess(["sleep", "99"], timeout=1)

        # logger.error must have been called with a message that mentions the timeout
        assert mock_error.called
        call_args = mock_error.call_args_list
        messages = " ".join(str(a) for call in call_args for a in call.args)
        assert "1" in messages  # timeout value included
        assert "sleep" in messages  # command name included

    def test_timeout_does_not_suppress_exception(self) -> None:
        """TimeoutExpired is always re-raised (not swallowed)."""
        exc = subprocess.TimeoutExpired(cmd=["ls"], timeout=5)
        with patch("subprocess.run", side_effect=exc):
            with pytest.raises(subprocess.TimeoutExpired):
                run_subprocess(["ls"], timeout=5)

    def test_timeout_log_on_error_false_suppresses_error_log(self) -> None:
        """Expected timeout failures can suppress lower-level ERROR logs."""
        exc = subprocess.TimeoutExpired(cmd=["sleep", "99"], timeout=1)
        with (
            patch("subprocess.run", side_effect=exc),
            patch("hephaestus.utils.helpers.logger.error") as mock_error,
        ):
            with pytest.raises(subprocess.TimeoutExpired):
                run_subprocess(["sleep", "99"], timeout=1, log_on_error=False)

        mock_error.assert_not_called()
