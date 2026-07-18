"""Tests for the thin ``hephaestus-drive-prs-green`` CLI wrapper (issue #1822).

``ci_driver.main()`` no longer runs a legacy ``CIDriver`` orchestration loop; it
parses the historical driver argument surface, builds a ``PipelineConfig``
trimmed to the ``(pr_review, merge_wait)`` stage scope, and dispatches to
``pipeline.coordinator.run_pipeline``. Seeding (issues / PRs / the repo-wide
failing-PR sweep) is coordinator-owned. These tests exercise the wrapper end to
end with ``run_pipeline`` (and repo resolution) mocked so no live agent or
GitHub call is made.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import ci_driver as ci_driver_mod
from hephaestus.automation.pipeline.routing import StageName


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def _run_main_capturing_config(argv: list[str], *, rc: int = 0) -> dict[str, Any]:
    """Run ``main()`` with ``argv``, capturing the PipelineConfig passed to run_pipeline.

    ``run_pipeline`` is stubbed to return ``rc``; ``_resolve_repo`` is pinned so
    the test never shells out to ``git``.
    """
    captured: dict[str, Any] = {}

    def _fake_run_pipeline(config: Any) -> int:
        captured["config"] = config
        return rc

    with (
        patch.object(sys, "argv", ["hephaestus-drive-prs-green", *argv]),
        patch.object(ci_driver_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(ci_driver_mod, "resolve_agent", return_value="claude"),
        patch(
            # main() does ``from .pipeline.coordinator import run_pipeline`` at
            # call time (a deferred import, not a module-level binding), so the
            # name must be patched on the coordinator module where it is
            # defined — patching ``ci_driver_mod.run_pipeline`` would miss it.
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=_fake_run_pipeline,
        ),
    ):
        result_rc = ci_driver_mod.main()

    captured["rc"] = result_rc
    return captured


class TestModuleSurface:
    """The wrapper keeps ``main`` and the slim ``CIDriver`` placeholder."""

    def test_main_callable(self) -> None:
        assert callable(ci_driver_mod.main)

    def test_cidriver_class_exposed(self) -> None:
        assert hasattr(ci_driver_mod, "CIDriver")


def test_main_builds_review_merge_wait_scope_and_dispatches() -> None:
    """--issues N builds a PR-review through merge-wait scoped config and returns rc."""
    captured = _run_main_capturing_config(["--issues", "123", "--dry-run"], rc=0)

    assert captured["rc"] == 0
    config = captured["config"]
    assert config.org == "acme"
    assert config.repos == ["widget"]
    assert config.issues == [123]
    assert config.dry_run is True
    # Direct PRs without an approval label must receive PR review first.
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PR_REVIEW, StageName.MERGE_WAIT})


def test_main_scoped_run_disables_drive_green_all() -> None:
    """A scoped run (--issues) must not widen to the repo-wide PR sweep."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"])

    assert captured["config"].drive_green_all is False


def test_main_prs_scope_disables_drive_green_all() -> None:
    """A --prs run stays narrow (no repo-wide sweep)."""
    captured = _run_main_capturing_config(["--prs", "661", "662", "--dry-run"])

    config = captured["config"]
    assert config.prs == [661, 662]
    assert config.drive_green_all is False


def test_main_discovery_mode_enables_drive_green_all() -> None:
    """No --issues/--prs enables the coordinator's repo-wide PR sweep."""
    captured = _run_main_capturing_config(["--dry-run"])

    config = captured["config"]
    assert config.issues == []
    assert config.prs == []
    assert config.drive_green_all is True


@pytest.mark.parametrize(
    ("argv", "include_bot_prs", "include_all_authors"),
    [
        pytest.param(["--dry-run"], True, False, id="defaults"),
        pytest.param(["--no-include-bot-prs", "--dry-run"], False, False, id="exclude-bots"),
        pytest.param(["--all", "--dry-run"], True, True, id="all-authors"),
        pytest.param(
            ["--all", "--no-include-bot-prs", "--dry-run"],
            False,
            True,
            id="all-non-bots",
        ),
    ],
)
def test_main_threads_drive_green_filter_flags(
    argv: list[str], include_bot_prs: bool, include_all_authors: bool
) -> None:
    """CLI discovery flags reach the pipeline configuration unchanged."""
    config = _run_main_capturing_config(argv)["config"]

    assert config.include_bot_prs is include_bot_prs
    assert config.include_all_authors is include_all_authors


def test_main_maps_max_workers_to_worker_pool() -> None:
    """--max-workers maps onto the pipeline worker-pool size."""
    captured = _run_main_capturing_config(["--issues", "5", "--max-workers", "7", "--dry-run"])

    assert captured["config"].max_workers == 7


def test_main_no_advise_propagates() -> None:
    """--no-advise maps to PipelineConfig.no_advise."""
    captured = _run_main_capturing_config(["--issues", "5", "--no-advise", "--dry-run"])

    assert captured["config"].no_advise is True


def test_main_dedupes_issue_and_pr_lists() -> None:
    """Duplicate --issues / --prs values collapse to first-seen-ordered sets."""
    captured = _run_main_capturing_config(
        ["--issues", "5", "5", "9", "5", "--prs", "1", "1", "2", "--dry-run"]
    )

    assert captured["config"].issues == [5, 9]
    assert captured["config"].prs == [1, 2]


def test_main_returns_run_pipeline_exit_code() -> None:
    """main() surfaces the coordinator's non-zero exit code verbatim."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"], rc=1)

    assert captured["rc"] == 1


def test_main_handles_keyboard_interrupt() -> None:
    """A KeyboardInterrupt out of run_pipeline maps to rc=130."""
    with (
        patch.object(sys, "argv", ["hephaestus-drive-prs-green", "--issues", "5"]),
        patch.object(ci_driver_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(ci_driver_mod, "resolve_agent", return_value="claude"),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=KeyboardInterrupt,
        ),
    ):
        rc = ci_driver_mod.main()

    assert rc == 130


def test_main_installs_sigtstp_handler() -> None:
    """main() fixes Ctrl+Z (#1784) via the shared install_sigtstp_only helper."""
    with patch("hephaestus.utils.terminal.install_sigtstp_only") as mock_tstp:
        captured = _run_main_capturing_config(["--issues", "5", "--dry-run"])

    assert captured["rc"] == 0
    mock_tstp.assert_called_once_with()
