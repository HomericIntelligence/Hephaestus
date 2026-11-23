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

import hephaestus.git.runtime as git_runtime


@pytest.fixture(autouse=True)
def _clear_runtime_caches() -> Generator[None]:
    """Isolate process-global repository caches for every runtime test."""
    git_runtime.clear_repo_caches()
    yield
    git_runtime.clear_repo_caches()


def _runtime_error_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Return rendered runtime error records without unrelated logger output."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "hephaestus.git.runtime" and record.levelno >= logging.ERROR
    ]


@pytest.mark.requires_posix
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
            caplog.at_level(logging.DEBUG, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime.run_subprocess", return_value=completed),
        ):
            result = git_runtime.run([sensitive_argument])

        assert result is completed
        assert sensitive_argument not in caplog.text

    def test_failed_command_logs_only_redacted_summary(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        sensitive_argument = "do-not-log-this-failed-command-argument"
        failure = subprocess.CalledProcessError(1, [sensitive_argument], stderr=sensitive_argument)
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime.run_subprocess", side_effect=failure),
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
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime.run_subprocess", side_effect=timeout),
            pytest.raises(subprocess.TimeoutExpired),
        ):
            git_runtime.run([sensitive_argument], timeout=60)

        assert "Subprocess timed out" in caplog.messages
        assert sensitive_argument not in caplog.text

    def test_git_command_delegates_to_shared_git_helper(self) -> None:
        completed = subprocess.CompletedProcess(["git"], 0, stdout="", stderr="")
        with patch("hephaestus.git.runtime._shared_run_git", return_value=completed) as mock_run:
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
            input_text=None,
            retries=0,
        )

    @pytest.mark.parametrize(
        ("returncode", "command", "cwd", "stdout", "stderr"),
        [
            pytest.param(
                128,
                ["git", "fetch", "origin"],
                Path("/repo/fetch"),
                "remote: fetch incomplete\n",
                "fatal: remote repository unavailable\n",
                id="exit-128",
            ),
            pytest.param(
                129,
                ["git", "worktree", "list", "--porcelain", "-z"],
                Path("/repo/usage"),
                "usage: git worktree list\n",
                "error: unknown switch `z'\n",
                id="exit-129",
            ),
        ],
    )
    def test_git_failure_logs_context_and_reraises(
        self,
        caplog: pytest.LogCaptureFixture,
        returncode: int,
        command: list[str],
        cwd: Path,
        stdout: str,
        stderr: str,
    ) -> None:
        failure = subprocess.CalledProcessError(returncode, command, output=stdout, stderr=stderr)
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=failure) as mock_run,
            pytest.raises(subprocess.CalledProcessError) as caught,
        ):
            git_runtime.run(command, cwd=cwd)

        assert caught.value is failure
        mock_run.assert_called_once_with(
            command,
            cwd=cwd,
            timeout=None,
            check=True,
            log_on_error=False,
            env=None,
            input_text=None,
            retries=0,
        )
        messages = _runtime_error_messages(caplog)
        assert len(messages) == 1
        message = messages[0]
        assert f"Subprocess failed with exit code {returncode}" in message
        assert " ".join(command) in message
        assert str(cwd) in message
        assert stdout.strip() in message
        assert stderr.strip() in message

    def test_git_failure_redacts_and_bounds_diagnostic_fields(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        command_url = "https://operator:command-credential@example.invalid/repository.git"
        command = ["git", "clone", "c" * 2200 + "\n" + command_url]
        cwd = Path("/repo/" + "d" * 2100 + "\ntoken=cwd-token-secret\ncwd-tail")
        private_key_type = "OPENSSH PRIVATE " + "KEY"
        private_key = (
            f"-----BEGIN {private_key_type}-----\n"
            "private-key-material\n"
            f"-----END {private_key_type}-----"
        )
        stdout = "s" * 2100 + "\n" + private_key + "\nstdout-tail"
        stderr = (
            "e" * 2100
            + "\napi_key=stderr-api-secret"
            + "\nAuthorization: Bearer stderr-auth-token\nstderr-tail"
        )
        failure = subprocess.CalledProcessError(128, command, output=stdout, stderr=stderr)
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=failure),
            pytest.raises(subprocess.CalledProcessError),
        ):
            git_runtime.run(command, cwd=cwd)

        messages = _runtime_error_messages(caplog)
        assert len(messages) == 1
        message = messages[0]
        assert "\n" not in message
        assert "\\n" in message
        command_text, remainder = message.split("; command=", 1)[1].split("; cwd=", 1)
        cwd_text, remainder = remainder.split("; stdout=", 1)
        stdout_text, stderr_text = remainder.split("; stderr=", 1)
        fields = {
            "command": ast.literal_eval(command_text),
            "cwd": ast.literal_eval(cwd_text),
            "stdout": ast.literal_eval(stdout_text),
            "stderr": ast.literal_eval(stderr_text),
        }
        for value in fields.values():
            assert value.startswith("...(")
            assert len(value) <= 2048
        assert fields["command"].endswith("\n<redacted-git-url>")
        assert fields["cwd"].endswith("cwd-tail")
        assert fields["stdout"].endswith("<redacted>\nstdout-tail")
        assert fields["stderr"].endswith("stderr-tail")
        assert "token=<redacted-value>" in fields["cwd"]
        assert "api_key=<redacted-value>" in fields["stderr"]
        assert "Authorization: Bearer <redacted-value>" in fields["stderr"]
        for secret in (
            command_url,
            "command-credential",
            "cwd-token-secret",
            "private-key-material",
            "OPENSSH PRIVATE KEY",
            "stderr-api-secret",
            "stderr-auth-token",
        ):
            assert secret not in message

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(
                subprocess.CalledProcessError(128, ["git", "status"], stderr="fatal"),
                id="called-process-error",
            ),
            pytest.param(subprocess.TimeoutExpired(["git", "status"], 3), id="timeout"),
        ],
    )
    def test_git_failure_redacts_url_shaped_path_cwd(
        self,
        caplog: pytest.LogCaptureFixture,
        failure: subprocess.CalledProcessError | subprocess.TimeoutExpired,
    ) -> None:
        cwd = Path("https://operator:synthetic-cwd-credential@example.invalid/repository")
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=failure),
            pytest.raises(type(failure)) as caught,
        ):
            git_runtime.run(["git", "status"], cwd=cwd)

        assert caught.value is failure
        messages = _runtime_error_messages(caplog)
        assert len(messages) == 1
        message = messages[0]
        assert "cwd='<redacted-git-url>'" in message
        assert "synthetic-cwd-credential" not in message
        assert str(cwd) not in message

    def test_git_timeout_logs_context_and_reraises(self, caplog: pytest.LogCaptureFixture) -> None:
        command = [
            "git",
            "fetch",
            "https://operator:timeout-credential@example.invalid/repository.git",
        ]
        timeout = subprocess.TimeoutExpired(
            command,
            7,
            output=b"output password=timeout-password\n",
            stderr=b"fatal: operation timed out\n",
        )
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=timeout),
            pytest.raises(subprocess.TimeoutExpired) as caught,
        ):
            git_runtime.run(command, cwd=Path("/repo/timeout"), timeout=7)

        assert caught.value is timeout
        messages = _runtime_error_messages(caplog)
        assert len(messages) == 1
        message = messages[0]
        assert message.startswith("Subprocess timed out")
        assert "exit code 128" not in message
        assert "exit code 129" not in message
        assert "<redacted-git-url>" in message
        assert "timeout-credential" not in message
        assert "/repo/timeout" in message
        assert "password=<redacted-value>" in message
        assert "timeout-password" not in message
        assert "fatal: operation timed out" in message

    @pytest.mark.parametrize(
        "failure",
        [
            pytest.param(
                subprocess.CalledProcessError(128, ["git", "status"], stderr="fatal"),
                id="called-process-error",
            ),
            pytest.param(subprocess.TimeoutExpired(["git", "status"], 3), id="timeout"),
        ],
    )
    def test_git_failure_logging_can_be_disabled(
        self,
        caplog: pytest.LogCaptureFixture,
        failure: subprocess.CalledProcessError | subprocess.TimeoutExpired,
    ) -> None:
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=failure) as mock_run,
            pytest.raises(type(failure)) as caught,
        ):
            git_runtime.run(["git", "status"], log_errors=False)

        assert caught.value is failure
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["log_on_error"] is False
        assert mock_run.call_args.kwargs["retries"] == 0
        assert _runtime_error_messages(caplog) == []

    def test_git_nonzero_result_without_check_stays_quiet(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        completed = subprocess.CompletedProcess(
            ["git", "status"], 128, stdout="", stderr="fatal: tolerated probe"
        )
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", return_value=completed),
        ):
            result = git_runtime.run(["git", "status"], check=False, log_errors=True)

        assert result is completed
        assert _runtime_error_messages(caplog) == []

    def test_real_shared_git_wrapper_does_not_duplicate_failure_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        failure = subprocess.CalledProcessError(128, ["git", "status"])
        with (
            caplog.at_level(logging.ERROR),
            patch("hephaestus.utils.git.run_subprocess", side_effect=failure) as mock_run,
            pytest.raises(subprocess.CalledProcessError) as caught,
        ):
            git_runtime.run(["git", "status"])

        assert caught.value is failure
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["log_on_error"] is False
        logger_names = {
            "hephaestus.git.runtime",
            "hephaestus.utils.git",
            "hephaestus.utils.helpers",
        }
        records = [
            record
            for record in caplog.records
            if record.name in logger_names and record.levelno >= logging.ERROR
        ]
        assert len(records) == 1
        assert records[0].name == "hephaestus.git.runtime"
        message = records[0].getMessage()
        assert "cwd='<current working directory>'" in message
        assert "stdout=" not in message
        assert "stderr=" not in message

    def test_git_cancellation_propagates_without_failure_log_or_retry(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        cancellation = InterruptedError("Git operation cancelled")
        with (
            caplog.at_level(logging.ERROR, logger="hephaestus.git.runtime"),
            patch("hephaestus.git.runtime._shared_run_git", side_effect=cancellation) as mock_run,
            pytest.raises(InterruptedError) as caught,
        ):
            git_runtime.run(["git", "status"])

        assert caught.value is cancellation
        mock_run.assert_called_once()
        assert _runtime_error_messages(caplog) == []


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
    with patch("hephaestus.git.runtime.get_repo_slug", return_value="hephaestus"):
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
