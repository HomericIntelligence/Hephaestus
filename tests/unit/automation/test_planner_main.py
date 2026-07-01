"""Smoke tests for ``Planner.run()`` and ``planner.main()``.

Captures the current end-to-end behavior of the planner CLI entry point so
the upcoming Planner-class decomposition (issue #598) can be verified as a
pure move-and-delegate refactor. These tests intentionally exercise a
narrow set of code paths through ``main()`` and ``Planner.run()`` and
mock all external collaborators (GitHub API + Claude calls).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import planner as planner_mod
from hephaestus.automation.models import DEFAULT_WORKER_COUNT, PlannerOptions, PlanResult


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def test_main_returns_zero_with_no_open_issues(monkeypatch: Any) -> None:
    """``main()`` with no --issues and no discovered issues exits 0."""
    monkeypatch.setattr("sys.argv", ["planner", "--dry-run", "--agent", "claude"])
    with patch(
        "hephaestus.automation.planner.gh_list_open_issues",
        return_value=[],
    ):
        rc = planner_mod.main()
    assert rc == 0


def test_main_resolves_agent_when_omitted(monkeypatch: Any) -> None:
    """PlannerOptions should receive the concrete auto-detected provider."""
    captured: dict[str, PlannerOptions] = {}

    class FakePlanner:
        def __init__(self, options: PlannerOptions) -> None:
            captured["options"] = options

        def run(self) -> dict[int, PlanResult]:
            return {123: PlanResult(issue_number=123, success=True)}

    monkeypatch.setattr("sys.argv", ["planner", "--issues", "123", "--dry-run"])
    with (
        patch("hephaestus.automation.planner.resolve_agent", return_value="codex") as mock_resolve,
        patch.object(planner_mod, "Planner", FakePlanner),
    ):
        rc = planner_mod.main()

    assert rc == 0
    mock_resolve.assert_called_once_with(None)
    assert captured["options"].agent == "codex"


def test_parse_args_default_parallel_uses_shared_worker_default() -> None:
    """Planner --parallel default stays aligned with shared worker defaults."""
    args = planner_mod._parse_args([])

    assert args.parallel == DEFAULT_WORKER_COUNT


def test_main_returns_zero_when_rate_limited(monkeypatch: Any, tmp_path: Path) -> None:
    """If issue discovery is rate-limited, main() exits cleanly and writes 0 work."""
    from hephaestus.automation.github_api import GitHubRateLimitError

    report = tmp_path / "report.txt"
    monkeypatch.setenv("HEPH_WORK_REPORT", str(report))
    monkeypatch.setattr("sys.argv", ["planner", "--agent", "claude"])
    with patch(
        "hephaestus.automation.planner.gh_list_open_issues",
        side_effect=GitHubRateLimitError("rate limit", reset_epoch=0),
    ):
        rc = planner_mod.main()
    assert rc == 0
    assert report.read_text(encoding="utf-8") == "0"


def test_run_skips_issue_with_existing_plan() -> None:
    """``Planner.run()`` short-circuits when the issue already has a plan.

    The dry_run flag is irrelevant here: the existing-plan guard runs
    BEFORE the dry-run branch, so neither a Claude call nor a GitHub post
    should occur.
    """
    options = PlannerOptions(
        issues=[123],
        dry_run=False,
        force=False,
        parallel=1,
        system_prompt_file=None,
        skip_closed=False,
        enable_advise=False,
    )
    planner = planner_mod.Planner(options)

    with (
        patch.object(planner, "_pr_coverage_skip", return_value=None),
        patch.object(planner, "_has_existing_plan", return_value=True) as mock_has,
        patch.object(planner, "_call_claude") as mock_claude,
        patch.object(planner, "_post_plan") as mock_post,
        # Isolate the pre-pass label filter from the live repo: without this the
        # batched fetch sees real issue #123's plan-go label and drops it before
        # the worker, so _has_existing_plan never runs (#1156). Empty labels →
        # the issue reaches the worker, where the mocked guard returns True.
        patch(
            "hephaestus.automation.state.planner.fetch_all_issue_labels_graphql",
            return_value={},
        ),
    ):
        results = planner.run()

    assert results[123].success is True
    assert results[123].plan_already_exists is True
    mock_has.assert_called_once_with(123)
    mock_claude.assert_not_called()
    mock_post.assert_not_called()


def test_run_dry_run_does_not_post_or_call_claude() -> None:
    """``--dry-run`` path: when no existing plan, run skips Claude + posting."""
    options = PlannerOptions(
        issues=[456],
        dry_run=True,
        force=True,  # bypass the existing-plan check
        parallel=1,
        system_prompt_file=None,
        skip_closed=False,
        enable_advise=False,
    )
    planner = planner_mod.Planner(options)

    with (
        patch.object(planner, "_pr_coverage_skip", return_value=None),
        patch.object(planner, "_call_claude") as mock_claude,
        patch.object(planner, "_post_plan") as mock_post,
    ):
        results = planner.run()

    assert results[456] == PlanResult(issue_number=456, success=True)
    mock_claude.assert_not_called()
    mock_post.assert_not_called()


def test_run_returns_empty_when_all_issues_filtered() -> None:
    """``Planner.run()`` returns ``{}`` when every issue is filtered out."""
    options = PlannerOptions(
        issues=[1, 2],
        dry_run=True,
        force=False,
        parallel=1,
        system_prompt_file=None,
        skip_closed=True,
        enable_advise=False,
    )
    planner = planner_mod.Planner(options)

    # Force _filter_issues to drop everything (simulates all-closed batch).
    with patch.object(planner, "_filter_issues", return_value=[]):
        results = planner.run()

    assert results == {}
