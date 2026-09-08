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
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from hephaestus.automation import ci_driver as ci_driver_mod
from hephaestus.automation.pipeline.routing import StageName


@pytest.fixture(autouse=True)
def _silence_logging(caplog: Any) -> None:
    """Keep test output tidy regardless of basicConfig calls in main()."""
    caplog.set_level("CRITICAL")


def _run_main_capturing_config(
    argv: list[str],
    *,
    rc: int = 0,
    resolved_agent: str = "claude",
    repository: tuple[str, str] = ("acme", "widget"),
) -> dict[str, Any]:
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
        patch.object(ci_driver_mod, "_resolve_repo", return_value=repository),
        patch.object(ci_driver_mod, "resolve_agent", return_value=resolved_agent),
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


def test_timeout_flags_thread_into_pipeline_config() -> None:
    """Standalone CI-driver timeout options configure scoped operations."""
    captured = _run_main_capturing_config(
        [
            "--issues",
            "123",
            "--agent-timeout",
            "11",
            "--poll-max-wait",
            "13",
        ]
    )
    config = captured["config"]
    assert (config.reviewer_timeout, config.implementer_timeout) == (11, 11)
    assert config.poll_max_wait == 13


def test_pi_directory_threads_into_pipeline_config(tmp_path: Path) -> None:
    """The CI workers must use the Pi directory that admission used."""
    captured = _run_main_capturing_config(
        ["--issues", "123", "--agent", "pi", "--pi-dir", str(tmp_path)],
        resolved_agent="pi",
    )

    assert captured["config"].pi_dir == tmp_path


def test_gh_extra_path_root_threads_into_pipeline_config(tmp_path: Path) -> None:
    """The validated GitHub CLI root reaches the production pool configuration."""
    gh_root = tmp_path / "gh-root"
    gh = gh_root / "bin" / "gh"
    gh.parent.mkdir(parents=True)
    gh.write_text("#!/bin/sh\n")
    gh.chmod(0o755)

    config = _run_main_capturing_config(["--issues", "123", "--gh-extra-path-root", str(gh_root)])[
        "config"
    ]

    assert config.gh_extra_path_root == gh_root.resolve()


def test_invalid_gh_extra_path_root_stops_before_github_work(tmp_path: Path) -> None:
    """An invalid GitHub CLI root stops before repository or pipeline access."""
    gh_root = tmp_path / "gh-root"
    gh_root.mkdir()

    with (
        patch.object(
            sys,
            "argv",
            ["hephaestus-drive-prs-green", "--gh-extra-path-root", str(gh_root)],
        ),
        patch.object(ci_driver_mod, "_resolve_repo") as resolve_repo,
        patch("hephaestus.automation.pipeline.coordinator.run_pipeline") as run_pipeline,
    ):
        with pytest.raises(SystemExit) as error:
            ci_driver_mod.main()

    assert error.value.code == 2
    resolve_repo.assert_not_called()
    run_pipeline.assert_not_called()


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_provider_owned_defaults_remain_empty(agent: str) -> None:
    """The CI wrapper must not inject Claude defaults into direct providers."""
    captured = _run_main_capturing_config(
        ["--issues", "123", "--agent", agent], resolved_agent=agent
    )

    config = captured["config"]
    assert (config.reviewer_model, config.fallback_model) == ("", "")


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
    """No --issues/--prs retains the linked-issue discovery compatibility flag."""
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
    """CLI compatibility fields reach the pipeline configuration unchanged."""
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


@pytest.mark.parametrize("model", ["terra-lite", "astra", "gpt-6-astra:max", "MixedCase"])
def test_literal_claude_model_reaches_pipeline_configuration(model: str) -> None:
    """The CLI keeps a supplied model name without an alias catalog."""
    captured = _run_main_capturing_config(
        ["--issues", "123", "--agent", "claude", "--reviewer-model", model]
    )
    assert captured["rc"] == 0
    assert captured["config"].reviewer_model == model


def test_ci_wrapper_forwards_codex_writer_isolation(tmp_path: Path) -> None:
    """Review remediation receives the operator's exact Codex adapter inputs."""
    lock = tmp_path / "deployment-lock.json"
    config = _run_main_capturing_config(
        [
            "--issues",
            "123",
            "--implementer-agent",
            "codex",
            "--codex-isolation-adapter",
            "test-adapter",
            "--codex-isolation-deployment-lock",
            str(lock),
            "--codex-isolation-deployment-lock-sha256",
            "a" * 64,
        ]
    )["config"]
    assert config.codex_isolation_adapter == "test-adapter"
    assert config.codex_isolation_deployment_lock == lock
    assert config.codex_isolation_deployment_lock_sha256 == "a" * 64


def test_bootstrap_comment_reaches_pipeline_config() -> None:
    """The CLI supplies only the selected comment ID to the pipeline."""
    captured = _run_main_capturing_config(
        ["--prs", "3006", "--host-verification-bootstrap-comment", "123"],
        repository=("HomericIntelligence", "Hephaestus"),
    )
    config = captured["config"]
    assert config.host_verification_bootstrap_comment_id == 123
    assert config.prs == [3006]
    assert config.issues == []
    assert config.drive_green_all is False


@pytest.mark.parametrize("repository", [("acme", "Hephaestus"), ("HomericIntelligence", "Other")])
def test_bootstrap_comment_rejects_other_repository(repository: tuple[str, str]) -> None:
    """The target repository is checked before the pipeline starts."""
    with (
        patch.object(
            sys,
            "argv",
            [
                "hephaestus-drive-prs-green",
                "--prs",
                "3006",
                "--host-verification-bootstrap-comment",
                "123",
            ],
        ),
        patch.object(ci_driver_mod, "_resolve_repo", return_value=repository),
        patch.object(ci_driver_mod, "resolve_agent", return_value="claude"),
        patch("hephaestus.automation.pipeline.coordinator.run_pipeline") as run,
    ):
        with pytest.raises(SystemExit) as error:
            ci_driver_mod.main()
        assert error.value.code == 2
        run.assert_not_called()
