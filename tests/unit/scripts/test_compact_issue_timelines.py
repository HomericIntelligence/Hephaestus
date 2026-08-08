"""CLI behavior for safe GitHub issue-timeline compaction."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from hephaestus.automation.review_journal import render_current_plan, render_current_review


@pytest.fixture
def script_module() -> ModuleType:
    """Load the standalone script as a testable module."""
    path = Path(__file__).parents[3] / "scripts" / "compact_issue_timelines.py"
    spec = importlib.util.spec_from_file_location("compact_issue_timelines", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_is_read_only_and_excludes_pull_requests(
    script_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default mode inventories all issues but performs no mutations."""
    comments = {
        1: [
            {"id": 11, "body": render_current_plan("Plan", revision=1), "user": {"login": "bot"}},
            {"id": 12, "body": render_current_review("GO", revision=1), "user": {"login": "bot"}},
            {
                "id": 13,
                "body": "<!-- hephaestus-state-skip-reason -->\nold",
                "user": {"login": "bot"},
            },
        ]
    }
    monkeypatch.setattr(
        script_module.github_api,
        "_gh_call",
        lambda _args: SimpleNamespace(stdout='[[{"number":1},{"number":2,"pull_request":{}}]]'),
    )
    monkeypatch.setattr(script_module.github_api, "gh_current_login", lambda: "bot")
    monkeypatch.setattr(
        script_module.github_api,
        "fetch_issue_comments_metadata",
        lambda issue, _repo: comments[issue],
    )
    mutations: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        script_module.github_api,
        "gh_issue_delete_comment",
        lambda *args, **kwargs: mutations.append((*args, kwargs)),
    )

    assert script_module.main(["--repo", "owner/repo"]) == 0

    assert mutations == []
    assert "DRY-RUN #1" in capsys.readouterr().out
