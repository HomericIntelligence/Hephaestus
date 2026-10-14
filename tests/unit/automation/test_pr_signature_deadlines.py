"""PR signature checks share the queue operation deadline and repository."""

from __future__ import annotations

import subprocess
from concurrent.futures import CancelledError
from pathlib import Path
from threading import Event
from typing import Any
from unittest.mock import Mock

import pytest

from hephaestus.automation import git_runtime, github_api
from hephaestus.automation.pipeline_github import PipelineGitHub


def _create_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: Mock
) -> PipelineGitHub:
    """Create an explicitly scoped adapter with no existing PR."""
    adapter = PipelineGitHub("owner", repo="repo", repo_root=tmp_path, command_runner=runner)
    monkeypatch.setattr(adapter, "_open_prs_for_branch", lambda _branch: [])
    monkeypatch.setattr(adapter, "find_pr_for_issue", lambda _issue: None)
    return adapter


def test_signature_git_children_share_the_operation_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fetch and range fallback consume one deadline in the selected checkout."""
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    shutdown = Event()
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def run_git(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        clock[0] += 3.0
        if argv[1] == "fetch":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if len(calls) == 2:
            return subprocess.CompletedProcess(argv, 128, "", "unknown remote ref")
        return subprocess.CompletedProcess(argv, 0, "abc123 G\n", "")

    monkeypatch.setattr(git_runtime, "_shared_run_git", run_git)
    runner = Mock(
        return_value=subprocess.CompletedProcess(
            [], 0, "https://github.com/owner/repo/pull/8\n", ""
        )
    )
    adapter = _create_adapter(tmp_path, monkeypatch, runner)

    with adapter.operation_deadline(110.0, shutdown=shutdown):
        assert adapter.create_pr(7, "feature", "title", "Closes #7") == 8

    assert [argv[-1] for argv, _kwargs in calls] == [
        "--quiet",
        "origin/main..origin/feature",
        "origin/main..feature",
    ]
    assert [kwargs["cwd"] for _argv, kwargs in calls] == [tmp_path] * 3
    assert [kwargs["timeout"] for _argv, kwargs in calls] == [10.0, 7.0, 4.0]
    assert all(kwargs.get("shutdown") is shutdown for _argv, kwargs in calls)
    assert runner.call_args.kwargs["deadline_s"] == 110.0
    assert runner.call_args.kwargs["timeout"] == 1.0
    assert runner.call_args.kwargs["shutdown"] is shutdown


@pytest.mark.parametrize("stop", ["deadline", "cancel"])
def test_signature_stop_prevents_later_git_and_create_commands(
    stop: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spent deadline or cancellation stops the signature sequence after fetch."""
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    shutdown = Event()
    commands: list[list[str]] = []

    def run_git(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(argv)
        if stop == "deadline":
            clock[0] = 111.0
        else:
            shutdown.set()
        return subprocess.CompletedProcess(argv, 0, "abc123 G\n", "")

    monkeypatch.setattr(git_runtime, "_shared_run_git", run_git)
    runner = Mock(side_effect=AssertionError("unexpected GitHub command"))
    adapter = _create_adapter(tmp_path, monkeypatch, runner)
    error = subprocess.TimeoutExpired if stop == "deadline" else CancelledError

    with adapter.operation_deadline(110.0, shutdown=shutdown), pytest.raises(error):
        adapter.create_pr(7, "feature", "title", "Closes #7")

    assert commands == [["git", "fetch", "origin", "main", "--quiet"]]
    runner.assert_not_called()


def test_signature_api_fallback_uses_explicit_repository_and_remaining_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Remote signature evidence uses the same scoped command runner and deadline."""
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    shutdown = Event()

    def run_git(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        clock[0] += 2.0
        return subprocess.CompletedProcess(argv, 0, "abc123 N\n" if argv[1] == "log" else "", "")

    def run_gh(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["api", "repos/owner/repo/commits/abc123"]:
            if "--repo" in argv:
                raise subprocess.CalledProcessError(1, argv, stderr="unknown flag: --repo")
            assert argv == [
                "api",
                "repos/owner/repo/commits/abc123",
                "--jq",
                ".commit.verification.verified",
            ]
            clock[0] += 2.0
            return subprocess.CompletedProcess(argv, 0, "true\n", "")
        assert argv[:2] == ["pr", "create"]
        return subprocess.CompletedProcess(argv, 0, "https://github.com/owner/repo/pull/8\n", "")

    monkeypatch.setattr(git_runtime, "_shared_run_git", run_git)
    monkeypatch.setattr(
        github_api, "get_repo_info", Mock(side_effect=AssertionError("ambient repository lookup"))
    )
    runner = Mock(side_effect=run_gh)
    adapter = _create_adapter(tmp_path, monkeypatch, runner)

    with adapter.operation_deadline(110.0, shutdown=shutdown):
        assert adapter.create_pr(7, "feature", "title", "Closes #7") == 8

    assert runner.call_count == 2
    assert [call.kwargs["timeout"] for call in runner.call_args_list] == [6.0, 4.0]
    assert all(call.kwargs["deadline_s"] == 110.0 for call in runner.call_args_list)
    assert all(call.kwargs["shutdown"] is shutdown for call in runner.call_args_list)
