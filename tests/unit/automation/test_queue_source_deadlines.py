"""Tests for bounded transport at lazy queue source boundaries."""

from __future__ import annotations

import json
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import loop_repo_manager
from hephaestus.automation.pipeline import admission
from hephaestus.automation.pipeline_github import PipelineGitHub


@pytest.mark.parametrize("source_kind", ["repositories", "issues"])
def test_source_page_has_one_attempt_and_a_fresh_deadline_after_idle_yield(
    monkeypatch: pytest.MonkeyPatch, source_kind: str
) -> None:
    """Idle time between pages does not use the next page's operation budget."""
    clock = [100.0]
    calls: list[dict[str, Any]] = []
    shutdown = threading.Event()

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kwargs)
        count = 100 if len(calls) == 1 else 1
        page = [
            {"name": f"repo-{number}", "archived": False, "fork": False}
            if source_kind == "repositories"
            else {"number": number + 1, "title": "Issue", "labels": []}
            for number in range(count)
        ]
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(page))

    monkeypatch.setattr(loop_repo_manager, "gh_call", run)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    source: Iterator[Any]
    if source_kind == "repositories":
        source = loop_repo_manager._iter_gh_repos("org", network_timeout=10, shutdown=shutdown)
    else:
        source = loop_repo_manager._iter_open_issue_meta(
            "org", "repo", network_timeout=10, shutdown=shutdown
        )
    for _ in range(100):
        next(source)
    assert len(calls) == 1
    clock[0] = 1000.0
    assert len(list(source)) == 1

    assert [options["deadline_s"] for options in calls] == [110.0, 1010.0]
    for options in calls:
        assert options["max_retries"] == 1
        assert options["retry_on_rate_limit"] is False
        assert options["shutdown"] is shutdown
        assert options["timeout"] == 10


def test_explicit_issue_closed_filter_uses_bounded_repository_reads(tmp_path: Path) -> None:
    """Issue state lookup cannot enter the library retry fallback."""
    calls: list[tuple[list[str], dict[str, Any]]] = []
    shutdown = threading.Event()
    deadline_s = time.monotonic() + 10

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        number = int(argv[2])
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps({"number": number, "state": "CLOSED" if number == 1 else "OPEN"}),
        )

    github = PipelineGitHub("org", repo="repo", repo_root=tmp_path, command_runner=run)

    assert admission._filter_open_issues(
        ("org", "repo"), [1, 2], github=github, deadline_s=deadline_s, shutdown=shutdown
    ) == [2]

    assert len(calls) == 2
    for _argv, options in calls:
        assert options["max_retries"] == 1
        assert options["retry_on_rate_limit"] is False
        assert options["deadline_s"] == deadline_s
        assert options["shutdown"] is shutdown
