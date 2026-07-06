"""Tests for the thin ``hephaestus-plan-issues`` CLI wrapper (issue #1820).

``planner.main()`` no longer runs a legacy ``Planner`` class; it parses the
historical planner argument surface, builds a ``PipelineConfig`` trimmed to the
``(planning, plan_review)`` stage scope, and dispatches to
``pipeline.coordinator.run_pipeline``. These tests exercise the wrapper end to
end with ``run_pipeline`` (and issue discovery) mocked so no live agent or
GitHub call is made.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import planner as planner_mod
from hephaestus.automation.models import DEFAULT_WORKER_COUNT
from hephaestus.automation.pipeline.routing import StageName


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def _run_main_capturing_config(argv: list[str], *, rc: int = 0) -> Any:
    """Run ``main()`` with ``argv``, capturing the PipelineConfig passed to run_pipeline.

    Returns the captured ``PipelineConfig`` instance. ``run_pipeline`` is
    stubbed to return ``rc`` and ``_resolve_repo`` is pinned so the test never
    shells out to ``git``.
    """
    captured: dict[str, Any] = {}

    def _fake_run_pipeline(config: Any) -> int:
        captured["config"] = config
        return rc

    with (
        patch("sys.argv", ["hephaestus-plan-issues", *argv]),
        patch.object(planner_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=_fake_run_pipeline,
        ),
        patch.object(planner_mod, "resolve_agent", return_value="claude"),
    ):
        result_rc = planner_mod.main()

    captured["rc"] = result_rc
    return captured


def test_parse_args_default_parallel_uses_shared_worker_default() -> None:
    """Planner --parallel default stays aligned with shared worker defaults."""
    args = planner_mod._parse_args([])

    assert args.parallel == DEFAULT_WORKER_COUNT


def test_main_builds_planning_scope_and_dispatches() -> None:
    """--issues N builds a (planning, plan_review) scoped config and returns run_pipeline's rc."""
    captured = _run_main_capturing_config(["--issues", "123", "--dry-run"], rc=0)

    assert captured["rc"] == 0
    config = captured["config"]
    assert config.org == "acme"
    assert config.repos == ["widget"]
    assert config.issues == [123]
    assert config.dry_run is True
    # Scope is trimmed to exactly planning + plan_review.
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})


def test_main_maps_parallel_to_worker_pool() -> None:
    """--parallel maps onto the pipeline worker-pool size."""
    captured = _run_main_capturing_config(["--issues", "5", "--parallel", "7", "--dry-run"])

    assert captured["config"].max_workers == 7


def test_main_force_sets_config_force() -> None:
    """--force maps to the seeding re-plan override on PipelineConfig."""
    captured = _run_main_capturing_config(["--issues", "5", "--force", "--dry-run"])

    assert captured["config"].force is True


def test_main_no_force_leaves_force_false() -> None:
    """Without --force the config force flag stays False."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"])

    assert captured["config"].force is False


def test_main_no_advise_propagates() -> None:
    """--no-advise maps to PipelineConfig.no_advise."""
    captured = _run_main_capturing_config(["--issues", "5", "--no-advise", "--dry-run"])

    assert captured["config"].no_advise is True


def test_main_dedupes_issue_list() -> None:
    """Duplicate --issues values are collapsed to a first-seen-ordered set."""
    captured = _run_main_capturing_config(["--issues", "5", "5", "9", "5", "--dry-run"])

    assert captured["config"].issues == [5, 9]


def test_main_returns_run_pipeline_exit_code() -> None:
    """main() surfaces the coordinator's non-zero exit code verbatim."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"], rc=1)

    assert captured["rc"] == 1


def test_main_discovers_open_issues_when_none_given() -> None:
    """With no --issues, main() seeds the discovered open-issue list."""
    with (
        patch("sys.argv", ["hephaestus-plan-issues", "--dry-run"]),
        patch.object(planner_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(planner_mod, "resolve_agent", return_value="claude"),
        patch(
            "hephaestus.automation.planner.gh_list_open_issues",
            return_value=[41, 42],
        ),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ) as mock_run,
    ):
        rc = planner_mod.main()

    assert rc == 0
    config = mock_run.call_args.args[0]
    assert config.issues == [41, 42]


def test_main_returns_zero_when_rate_limited() -> None:
    """If issue discovery is rate-limited, main() exits cleanly without dispatching."""
    from hephaestus.automation.github_api import GitHubRateLimitError

    with (
        patch("sys.argv", ["hephaestus-plan-issues", "--agent", "claude"]),
        patch.object(planner_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(planner_mod, "resolve_agent", return_value="claude"),
        patch(
            "hephaestus.automation.planner.gh_list_open_issues",
            side_effect=GitHubRateLimitError("rate limit", reset_epoch=0),
        ),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ) as mock_run,
    ):
        rc = planner_mod.main()

    assert rc == 0
    mock_run.assert_not_called()
