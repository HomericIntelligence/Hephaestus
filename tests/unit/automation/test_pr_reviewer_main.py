"""Tests for the thin ``hephaestus-review-prs`` CLI wrapper (issue #1823).

``pr_reviewer.main()`` no longer runs a legacy ``PRReviewer`` class; it parses
the historical reviewer argument surface, builds a ``PipelineConfig`` trimmed to
the ``pr_review`` stage scope, and dispatches to
``pipeline.coordinator.run_pipeline``. These tests exercise the wrapper end to
end with ``run_pipeline`` mocked so no live agent or GitHub call is made.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import pr_reviewer as pr_reviewer_mod
from hephaestus.automation.pipeline.routing import StageName


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def _run_main_capturing_config(argv: list[str], *, rc: int = 0) -> dict[str, Any]:
    """Run ``main()`` with ``argv``, capturing the PipelineConfig passed to run_pipeline.

    Returns a dict with the captured ``config`` and the returned ``rc``.
    ``run_pipeline`` is stubbed to return ``rc`` and ``_resolve_repo`` is pinned
    so the test never shells out to ``git``.
    """
    captured: dict[str, Any] = {}

    def _fake_run_pipeline(config: Any) -> int:
        captured["config"] = config
        return rc

    with (
        patch("sys.argv", ["hephaestus-review-prs", *argv]),
        patch.object(pr_reviewer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=_fake_run_pipeline,
        ),
        patch.object(pr_reviewer_mod, "resolve_agent", return_value="claude"),
    ):
        captured["rc"] = pr_reviewer_mod.main()
    return captured


def test_main_builds_pr_review_scope_and_dispatches() -> None:
    """--issues N builds a pr_review-scoped config and returns run_pipeline's rc."""
    captured = _run_main_capturing_config(["--issues", "123", "--dry-run"], rc=0)

    assert captured["rc"] == 0
    config = captured["config"]
    assert config.org == "acme"
    assert config.repos == ["widget"]
    assert config.issues == [123]
    assert config.dry_run is True
    # Scope is trimmed to exactly the single pr_review stage.
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PR_REVIEW})


def test_main_maps_max_workers_to_worker_pool() -> None:
    """--max-workers maps onto the pipeline worker-pool size."""
    captured = _run_main_capturing_config(["--issues", "5", "--max-workers", "4", "--dry-run"])

    assert captured["config"].max_workers == 4


def test_main_dedupes_issue_list() -> None:
    """Duplicate --issues values are collapsed to a first-seen-ordered set."""
    captured = _run_main_capturing_config(["--issues", "5", "5", "9", "5", "--dry-run"])

    assert captured["config"].issues == [5, 9]


def test_main_returns_run_pipeline_exit_code() -> None:
    """main() surfaces the coordinator's non-zero exit code verbatim."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"], rc=1)

    assert captured["rc"] == 1


def test_main_returns_130_on_keyboard_interrupt() -> None:
    """A KeyboardInterrupt during run_pipeline is caught and returns 130."""
    with (
        patch("sys.argv", ["hephaestus-review-prs", "--issues", "1", "--dry-run"]),
        patch.object(pr_reviewer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(pr_reviewer_mod, "resolve_agent", return_value="claude"),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=KeyboardInterrupt(),
        ),
    ):
        assert pr_reviewer_mod.main() == 130


def test_parse_args_requires_issues() -> None:
    """The reviewer CLI requires --issues (build_review_parser sets required=True)."""
    with pytest.raises(SystemExit):
        pr_reviewer_mod._parse_args([])
