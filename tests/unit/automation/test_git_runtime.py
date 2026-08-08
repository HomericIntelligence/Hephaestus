"""Tests for dependency-neutral Git execution and repository identity helpers."""

import ast
import logging
import subprocess
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

import hephaestus.automation.git_runtime as git_runtime


@pytest.fixture(autouse=True)
def _clear_runtime_caches() -> Generator[None]:
    """Isolate process-global repository caches for every runtime test."""
    git_runtime.clear_repo_caches()
    yield
    git_runtime.clear_repo_caches()


@pytest.mark.requires_posix
@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="POSIX coreutils are not guaranteed on win32 (#742)",
)
class TestRun:
    """Tests for subprocess delegation and redacted diagnostics."""

    def test_successful_command(self) -> None:
        result = git_runtime.run(["echo", "hello"], check=True, capture_output=True)

        assert result.returncode == 0
        assert "hello" in result.stdout

    def test_command_debug_log_does_not_disclose_arguments(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensitive_argument = "do-not-log-this-command-argument"
        completed = subprocess.CompletedProcess([sensitive_argument], 0, stdout="", stderr="")
        with (
            caplog.at_level(logging.DEBUG, logger="hephaestus.automation.git_runtime"),
            patch("hephaestus.automation.git_runtime.run_subprocess", return_value=completed),
        ):
            result = git_runtime.run([sensitive_argument])

        assert result is completed
        assert "Running subprocess" in caplog.messages
        assert sensitive_argument not in caplog.text

    def test_failed_command_logs_only_redacted_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensitive_argument = "do-not-log-this-failed-command-argument"
        failure = subprocess.CalledProcessError(1, [sensitive_argument], stderr=sensitive_argument)
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.automation.git_runtime"),
            patch("hephaestus.automation.git_runtime.run_subprocess", side_effect=failure),
            pytest.raises(subprocess.CalledProcessError),
        ):
            git_runtime.run([sensitive_argument])

        assert "Subprocess failed with exit code 1" in caplog.messages
        assert sensitive_argument not in caplog.text

    def test_timed_out_command_logs_only_redacted_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensitive_argument = "do-not-log-this-timed-out-command-argument"
        timeout = subprocess.TimeoutExpired([sensitive_argument], 60)
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.automation.git_runtime"),
            patch("hephaestus.automation.git_runtime.run_subprocess", side_effect=timeout),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            git_runtime.run([sensitive_argument], timeout=60)

        assert "Subprocess timed out" in caplog.messages
        assert sensitive_argument not in caplog.text

    def test_git_command_delegates_to_shared_git_helper(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch(
            "hephaestus.automation.git_runtime._shared_run_git", return_value=completed
        ) as mock_run:
            result = git_runtime.run(
                ["git", "status"],
                cwd=Path("/repo"),
                check=False,
                timeout=42,
                log_errors=False,
            )

        assert result is completed
        mock_run.assert_called_once_with(
            ["git", "status"],
            cwd=Path("/repo"),
            timeout=42,
            check=False,
            log_on_error=False,
            env=None,
            retries=0,
        )


def test_get_repo_root_uses_canonical_helper(tmp_path: Path) -> None:
    """Resolve the repository root through the canonical helper."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    with patch("hephaestus.utils.helpers.Path.cwd", return_value=nested):
        assert git_runtime.get_repo_root() == repo


def test_get_repo_root_returns_path(tmp_path: Path) -> None:
    """The compatibility helper returns a ``Path`` object."""
    assert isinstance(git_runtime.get_repo_root(tmp_path), Path)


@pytest.mark.parametrize(
    "remote_url",
    ["git@github.com:owner/repo.git", "https://github.com/owner/repo.git"],
)
def test_get_repo_info_parses_remote_urls(git_runtime_mocks: Any, remote_url: str) -> None:
    """Parse both supported GitHub remote URL forms."""
    mocks = git_runtime_mocks
    mocks.repo_root.return_value = Path("/home/user/repo")
    mocks.run.return_value = SimpleNamespace(stdout=f"{remote_url}\n")

    assert git_runtime.get_repo_info() == ("owner", "repo")


def test_get_repo_info_rejects_invalid_remote(git_runtime_mocks: Any) -> None:
    """Reject remotes that do not use a supported URL shape."""
    mocks = git_runtime_mocks
    mocks.repo_root.return_value = Path("/home/user/repo")
    mocks.run.return_value = SimpleNamespace(stdout="invalid-url\n")

    with pytest.raises(RuntimeError, match="Unable to parse git remote URL"):
        git_runtime.get_repo_info()


def test_result_caching_prevents_repeated_git_queries(git_runtime_mocks: Any) -> None:
    """Cache repeated repository-info lookups by resolved checkout path."""
    mocks = git_runtime_mocks
    repo_root = Path("/repo")
    mocks.run.return_value = SimpleNamespace(stdout="git@github.com:owner/repo.git\n")

    assert git_runtime.get_repo_info(repo_root) == ("owner", "repo")
    assert git_runtime.get_repo_info(repo_root) == ("owner", "repo")
    assert mocks.run.call_count == 1


def test_clear_repo_caches_forces_re_detection(git_runtime_mocks: Any) -> None:
    """Clearing caches makes the next repository lookup execute Git again."""
    mocks = git_runtime_mocks
    repo_root = Path("/repo")
    mocks.run.return_value = SimpleNamespace(stdout="git@github.com:owner/repo.git\n")

    git_runtime.get_repo_info(repo_root)
    git_runtime.clear_repo_caches()
    git_runtime.get_repo_info(repo_root)

    assert mocks.run.call_count == 2


def test_get_repo_slug_falls_back_when_remote_is_unavailable(
    git_runtime_mocks: Any,
) -> None:
    """Return the stable fallback slug when the remote cannot be read."""
    mocks = git_runtime_mocks
    mocks.run.side_effect = subprocess.CalledProcessError(1, ["git"])

    assert git_runtime.get_repo_slug(Path("/repo")) == "repo"


def test_reference_helpers_use_repository_slug() -> None:
    """Format issue and pull-request references from the current slug."""
    with patch("hephaestus.automation.git_runtime.get_repo_slug", return_value="hephaestus"):
        assert git_runtime.issue_ref(7) == "hephaestus#7"
        assert git_runtime.pr_ref("8") == "hephaestus#8"


def test_runtime_has_no_automation_imports() -> None:
    """Keep the neutral runtime module independent from automation modules."""
    tree = ast.parse(Path(git_runtime.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                imported.add(f"{'.' * node.level}{node.module or ''}")
            elif node.module:
                imported.add(node.module)

    assert not any(
        name.startswith("hephaestus.automation") or name.startswith(".") for name in imported
    )
