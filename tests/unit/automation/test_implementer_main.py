"""Tests for the thin ``hephaestus-implement-issues`` CLI wrapper (issue #1821).

``implementer.main()`` no longer runs a legacy ``IssueImplementer`` orchestration
loop; it parses the historical implementer argument surface, builds a
``PipelineConfig`` trimmed to the ``(implementation, pr_review, strict_review)``
stage scope, seeds the requested (or discovered) issues, and dispatches to
``pipeline.coordinator.run_pipeline``. These tests exercise the wrapper end to
end with ``run_pipeline`` (and issue discovery) mocked so no live agent or
GitHub call is made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import implementer as implementer_mod
from hephaestus.automation.pipeline.routing import StageName


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def _run_main_capturing_config(argv: list[str], tmp_path: Path, *, rc: int = 0) -> dict[str, Any]:
    """Run ``main()`` with ``argv``, capturing the PipelineConfig passed to run_pipeline.

    ``run_pipeline`` is stubbed to return ``rc``; ``_resolve_repo`` and
    ``get_repo_root`` are pinned so the test never shells out to ``git``.
    """
    captured: dict[str, Any] = {}

    def _fake_run_pipeline(config: Any) -> int:
        captured["config"] = config
        return rc

    with (
        patch.object(sys, "argv", ["hephaestus-implement-issues", *argv]),
        patch.object(implementer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(implementer_mod, "get_repo_root", return_value=tmp_path),
        patch.object(implementer_mod, "resolve_agent", return_value="claude"),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=_fake_run_pipeline,
        ),
    ):
        result_rc = implementer_mod.main()

    captured["rc"] = result_rc
    return captured


class TestModuleSurface:
    """The wrapper keeps ``main`` and the slim ``IssueImplementer`` importable."""

    def test_main_callable(self) -> None:
        assert callable(implementer_mod.main)

    def test_implementer_class_exposed(self) -> None:
        assert hasattr(implementer_mod, "IssueImplementer")

    def test_claude_impl_timeout_constant_exposed(self) -> None:
        assert isinstance(implementer_mod._CLAUDE_IMPL_TIMEOUT, int)


def test_main_builds_implementation_scope_and_dispatches(tmp_path: Path) -> None:
    """--issues N builds an (implementation, pr_review, strict_review) scoped config."""
    captured = _run_main_capturing_config(["--issues", "123", "--dry-run"], tmp_path, rc=0)

    assert captured["rc"] == 0
    config = captured["config"]
    assert config.org == "acme"
    assert config.repos == ["widget"]
    assert config.issues == [123]
    assert config.dry_run is True
    # Scope is trimmed to exactly implementation + pr_review + strict_review.
    assert config.scope is not None
    assert config.scope.stages == frozenset(
        {StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.STRICT_REVIEW}
    )


def test_main_maps_max_workers_to_worker_pool(tmp_path: Path) -> None:
    """--max-workers maps onto the pipeline worker-pool size."""
    captured = _run_main_capturing_config(
        ["--issues", "5", "--max-workers", "7", "--dry-run"], tmp_path
    )

    assert captured["config"].max_workers == 7


def test_main_no_advise_propagates(tmp_path: Path) -> None:
    """--no-advise maps to PipelineConfig.no_advise."""
    captured = _run_main_capturing_config(["--issues", "5", "--no-advise", "--dry-run"], tmp_path)

    assert captured["config"].no_advise is True


def test_main_nitpick_propagates(tmp_path: Path) -> None:
    """--nitpick maps to PipelineConfig.nitpick."""
    captured = _run_main_capturing_config(["--issues", "5", "--nitpick", "--dry-run"], tmp_path)

    assert captured["config"].nitpick is True


def test_main_dedupes_issue_list(tmp_path: Path) -> None:
    """Duplicate --issues values are collapsed to a first-seen-ordered set."""
    captured = _run_main_capturing_config(["--issues", "5", "5", "9", "5", "--dry-run"], tmp_path)

    assert captured["config"].issues == [5, 9]


def test_main_returns_run_pipeline_exit_code(tmp_path: Path) -> None:
    """main() surfaces the coordinator's non-zero exit code verbatim."""
    captured = _run_main_capturing_config(["--issues", "5", "--dry-run"], tmp_path, rc=1)

    assert captured["rc"] == 1


def test_main_installs_sigtstp_handler(tmp_path: Path) -> None:
    """main() fixes Ctrl+Z (#1784) via the shared install_sigtstp_only helper."""
    with patch("hephaestus.utils.terminal.install_sigtstp_only") as mock_tstp:
        captured = _run_main_capturing_config(["--issues", "5", "--dry-run"], tmp_path)

    assert captured["rc"] == 0
    mock_tstp.assert_called_once_with()


def test_main_discovers_open_issues_when_none_given(tmp_path: Path) -> None:
    """With no --issues/--epic, main() seeds the discovered open-issue list."""
    with (
        patch.object(sys, "argv", ["hephaestus-implement-issues", "--dry-run"]),
        patch.object(implementer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(implementer_mod, "get_repo_root", return_value=tmp_path),
        patch.object(implementer_mod, "resolve_agent", return_value="claude"),
        patch.object(implementer_mod, "gh_list_open_issues", return_value=[41, 42]),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ) as mock_run,
    ):
        rc = implementer_mod.main()

    assert rc == 0
    config = mock_run.call_args.args[0]
    assert config.issues == [41, 42]


def test_main_returns_zero_when_rate_limited(tmp_path: Path) -> None:
    """If issue discovery is rate-limited, main() exits cleanly without dispatching."""
    from hephaestus.automation.github_api import GitHubRateLimitError

    with (
        patch.object(sys, "argv", ["hephaestus-implement-issues"]),
        patch.object(implementer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(implementer_mod, "get_repo_root", return_value=tmp_path),
        patch.object(implementer_mod, "resolve_agent", return_value="claude"),
        patch.object(
            implementer_mod,
            "gh_list_open_issues",
            side_effect=GitHubRateLimitError("rate limit", reset_epoch=0),
        ),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ) as mock_run,
    ):
        rc = implementer_mod.main()

    assert rc == 0
    mock_run.assert_not_called()


def test_main_health_check_short_circuits(tmp_path: Path) -> None:
    """--health-check runs the environment probe and never dispatches to the pipeline."""
    with (
        patch.object(sys, "argv", ["hephaestus-implement-issues", "--health-check"]),
        patch.object(implementer_mod, "get_repo_root", return_value=tmp_path),
        patch("hephaestus.github.client.gh_call", side_effect=OSError("missing gh")),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ) as mock_run,
    ):
        rc = implementer_mod.main()

    assert rc == 0
    mock_run.assert_not_called()


def test_main_configures_logging_under_default_state_dir(tmp_path: Path) -> None:
    """main() passes the canonical state directory to logging setup."""
    from hephaestus.automation._review_utils import DEFAULT_STATE_DIR

    with (
        patch.object(sys, "argv", ["hephaestus-implement-issues", "--issues", "5", "--dry-run"]),
        patch.object(implementer_mod, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(implementer_mod, "get_repo_root", return_value=tmp_path),
        patch.object(implementer_mod, "resolve_agent", return_value="claude"),
        patch.object(implementer_mod, "_setup_logging") as mock_setup_logging,
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            return_value=0,
        ),
    ):
        rc = implementer_mod.main()

    assert rc == 0
    mock_setup_logging.assert_called_once()
    assert mock_setup_logging.call_args.kwargs["log_dir"] == tmp_path / DEFAULT_STATE_DIR
