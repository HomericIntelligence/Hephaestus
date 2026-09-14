"""Pipeline dispatch tests for loop_runner.main.

The queue-based pipeline is the only automation-loop path (epic #1809, cutover
#1818, legacy-path removal #1819). ``loop_runner.main`` parses the CLI, builds a
``PipelineConfig``, runs a repo-token preflight, and hands off to
``run_pipeline``. The repo stage owns cloning, so ``main`` does not clone
(C3: no double-clone).
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock

import pytest

import hephaestus.automation.event_log_io as event_log_io
import hephaestus.automation.pipeline.coordinator as coordinator_mod
import hephaestus.automation.pipeline_cli as loop_runner
from hephaestus.agents.model_selection import parse_model_selection
from hephaestus.automation.event_log_retention import (
    DEFAULT_EVENT_LOG_RETENTION_COUNT,
    DEFAULT_EVENT_LOG_RETENTION_DAYS,
    event_log_lifecycle,
)
from hephaestus.automation.pipeline.routing import StageName
from hephaestus.automation.pipeline.stages.base import StageContext, stage_model
from hephaestus.automation.pipeline.worker_pool import WorkerPool
from hephaestus.config.paths import DEFAULT_PROJECTS_DIR


@pytest.fixture
def dispatch(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Patch the pipeline dispatch target and the pre-dispatch collaborators."""
    mocks = {
        "run_pipeline": MagicMock(return_value=0),
        "preflight": MagicMock(),
        "submit": MagicMock(),
        "event_log_lifecycle": MagicMock(),
    }
    mocks["event_log_lifecycle"].return_value.__enter__.side_effect = lambda: SimpleNamespace(
        path=mocks["event_log_lifecycle"].call_args.args[0]
    )
    mocks["event_log_lifecycle"].return_value.__exit__.return_value = False
    monkeypatch.setattr(coordinator_mod, "run_pipeline", mocks["run_pipeline"])
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", mocks["preflight"])
    monkeypatch.setattr(WorkerPool, "submit", mocks["submit"])
    monkeypatch.setattr(loop_runner, "event_log_lifecycle", mocks["event_log_lifecycle"])
    monkeypatch.setattr(
        loop_runner, "_resolve_org_and_repos", lambda args: ("org", ["repo-a"], None)
    )
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent, **_kwargs: "claude")
    return mocks


def test_main_dispatches_to_pipeline(dispatch: dict[str, MagicMock]) -> None:
    """main() always runs the queue-based pipeline."""
    exit_code = loop_runner.main([])

    assert exit_code == 0
    dispatch["run_pipeline"].assert_called_once()


def test_pipeline_path_preflights_but_skips_clone(dispatch: dict[str, MagicMock]) -> None:
    """C3: pipeline keeps token preflight, while the repo stage owns cloning."""
    loop_runner.main([])

    dispatch["run_pipeline"].assert_called_once()
    dispatch["preflight"].assert_called_once_with("org", "repo-a", timeout=120)
    dispatch["submit"].assert_not_called()


def test_pipeline_exit_code_propagates(dispatch: dict[str, MagicMock]) -> None:
    """run_pipeline's exit code IS main's exit code."""
    dispatch["run_pipeline"].return_value = 130

    assert loop_runner.main([]) == 130


def test_dry_run_skips_preflight(dispatch: dict[str, MagicMock]) -> None:
    """A dry run must not hit the live gh token preflight."""
    loop_runner.main(["--dry-run"])

    dispatch["run_pipeline"].assert_called_once()
    dispatch["preflight"].assert_not_called()


def test_build_pipeline_config_maps_cli_fields(
    dispatch: dict[str, MagicMock],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_build_pipeline_config carries the CLI scope into PipelineConfig."""
    projects_dir = tmp_path / "projects"
    user_home = tmp_path / "user-home"
    host_temp = tmp_path / "host-temp"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))
    loop_runner.main(
        [
            "--projects-dir",
            str(projects_dir),
            "--loops",
            "3",
            "--max-workers",
            "4",
            "--parallel-repos",
            "2",
            "--dry-run",
            "--issues",
            "11,12",
            "--prs",
            "21,22",
            "--no-advise",
            "--no-serialize-file-overlap",
            "--nitpick",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.org == "org"
    assert config.repos == ["repo-a"]
    assert config.issues == [11, 12]
    assert config.prs == [21, 22]
    assert config.loops == 3
    assert config.max_workers == 4
    assert config.parallel_repos == 2
    assert config.dry_run is True
    assert config.no_advise is True
    assert config.serialize_file_overlap is False
    assert config.nitpick is True
    assert config.scope is None
    assert config.projects_dir == projects_dir.resolve()
    assert config.event_log_path is not None
    assert config.event_log_path.name.startswith("pipeline-events-")
    assert config.event_log_path.parent == (
        user_home / ".hephaestus-diagnostics" / config.projects_dir.name
    )
    dispatch["event_log_lifecycle"].assert_called_once_with(
        config.event_log_path,
        retention_days=DEFAULT_EVENT_LOG_RETENTION_DAYS,
        retention_count=DEFAULT_EVENT_LOG_RETENTION_COUNT,
        dry_run=True,
        candidates=config.event_log_candidates,
    )


def test_build_pipeline_config_maps_pyxis_image_path(
    dispatch: dict[str, MagicMock], tmp_path: Path
) -> None:
    """The loop exposes the local Pyxis image override to the pipeline."""
    image = tmp_path / "hephaestus-ci.sqsh"

    authority = tmp_path / "authority.json"
    quota_root = tmp_path / "quota"
    digest = "a" * 64
    loop_runner.main(
        [
            "--host-verification-pyxis-image",
            str(image),
            "--host-verification-pyxis-sha256",
            digest,
            "--host-verification-pyxis-authority",
            str(authority),
            "--host-verification-pyxis-quota-root",
            str(quota_root),
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.host_verification_pyxis_image == image
    assert config.host_verification_pyxis_sha256 == digest
    assert config.host_verification_pyxis_authority == authority
    assert config.host_verification_pyxis_quota_root == quota_root


def test_event_log_retention_flags_reach_lifecycle(
    dispatch: dict[str, MagicMock],
) -> None:
    """Custom retention settings are passed to the lifecycle wrapper."""
    loop_runner.main(
        [
            "--dry-run",
            "--event-log-retention-days",
            "14",
            "--event-log-retention-count",
            "25",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    dispatch["event_log_lifecycle"].assert_called_once_with(
        config.event_log_path,
        retention_days=14,
        retention_count=25,
        dry_run=True,
        candidates=config.event_log_candidates,
    )


def test_build_pipeline_config_maps_explicit_gh_root(
    dispatch: dict[str, MagicMock], tmp_path: Path
) -> None:
    """The CLI-only executable exception reaches the checkout worker config."""
    gh_root = tmp_path / "gh-root"
    executable = gh_root / "bin" / "gh"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)

    loop_runner.main(["--gh-extra-path-root", str(gh_root)])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.gh_extra_path_root == gh_root


@pytest.mark.parametrize("repository", ("repo-a", "build"))
def test_default_pipeline_event_log_path_does_not_create_repo_checkout(
    repository: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default event log path must not live under a repo clone directory."""
    user_home = tmp_path / "user-home"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    path = loop_runner._pipeline_event_log_path(DEFAULT_PROJECTS_DIR, [repository])

    assert path is not None
    assert path.parent == user_home / ".hephaestus-diagnostics" / DEFAULT_PROJECTS_DIR.name
    assert DEFAULT_PROJECTS_DIR / repository not in path.parents


def test_default_pipeline_event_log_path_does_not_require_projects_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default event log uses user storage instead of the projects parent."""
    user_home = tmp_path / "user-home"
    projects_dir = tmp_path / "read-only-parent" / "projects"
    user_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))

    path = loop_runner._pipeline_event_log_path(projects_dir, ["repo-a"])

    assert path is not None
    assert path.parent == user_home / ".hephaestus-diagnostics" / projects_dir.name
    assert projects_dir.parent not in path.parents


def test_default_pipeline_event_log_path_uses_temp_when_home_is_projects_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo named for diagnostics cannot collide with the home candidate."""
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    path = loop_runner._pipeline_event_log_path(
        projects_dir,
        [".hephaestus-diagnostics"],
    )

    assert path is not None
    assert path.parent == (
        host_temp / f"hephaestus-{os.geteuid()}" / ".hephaestus-diagnostics" / projects_dir.name
    )
    assert projects_dir.resolve() not in path.resolve().parents


def test_default_pipeline_event_log_path_canonicalizes_trusted_temp_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A symlinked provider prefix resolves before the private namespace."""
    projects_dir = tmp_path / "projects"
    canonical_temp = tmp_path / "canonical-temp"
    provider_temp = tmp_path / "provider-temp"
    attacker = tmp_path / "attacker"
    canonical_temp.mkdir()
    attacker.mkdir()
    provider_temp.symlink_to(canonical_temp, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(provider_temp))

    candidates = loop_runner._pipeline_event_log_candidates(projects_dir, ["repo-a"])
    path = loop_runner._select_pipeline_event_log_path(candidates)

    assert path is not None
    namespace = canonical_temp / f"hephaestus-{os.geteuid()}"
    assert path.parent == namespace / ".hephaestus-diagnostics" / projects_dir.name
    with event_log_lifecycle(
        path,
        retention_days=0,
        retention_count=0,
        dry_run=False,
        candidates=candidates,
    ) as handle:
        assert handle is not None
        provider_temp.unlink()
        provider_temp.symlink_to(attacker, target_is_directory=True)
        handle.append_line('{"event":"probe"}\n')

    assert path.is_file()
    assert not (attacker / f"hephaestus-{os.geteuid()}").exists()


def test_default_pipeline_event_log_path_disables_candidates_in_projects_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The optional event log stays off if each candidate can block intake."""
    projects_dir = tmp_path / "projects"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(
        tempfile,
        "gettempdir",
        lambda: str(projects_dir / "host-temp"),
    )

    with caplog.at_level(logging.WARNING, logger=loop_runner.LOG.name):
        path = loop_runner._pipeline_event_log_path(
            projects_dir,
            [".hephaestus-diagnostics"],
        )

    assert path is None
    assert any("event logging is disabled" in record.message for record in caplog.records)


def test_default_pipeline_event_log_path_disables_unverified_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The optional event log stays off when all candidates are in a worktree."""
    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: checkout))
    monkeypatch.setattr(
        tempfile,
        "gettempdir",
        lambda: str(checkout / "host-temp"),
    )

    with caplog.at_level(logging.WARNING, logger=loop_runner.LOG.name):
        path = loop_runner._pipeline_event_log_path(tmp_path / "projects", ["repo-a"])

    assert path is None
    assert any("event logging is disabled" in record.message for record in caplog.records)


def test_default_pipeline_event_log_path_uses_temp_when_home_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed home lookup does not hide a safe temporary directory."""
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()

    def unavailable_home(cls: type[Path]) -> Path:
        del cls
        raise OSError("home lookup failed")

    monkeypatch.setattr(Path, "home", classmethod(unavailable_home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    with caplog.at_level(logging.WARNING, logger=loop_runner.LOG.name):
        path = loop_runner._pipeline_event_log_path(tmp_path / "projects", ["repo-a"])

    assert path is not None
    assert path.parent == (
        host_temp / f"hephaestus-{os.geteuid()}" / ".hephaestus-diagnostics" / "projects"
    )
    assert not caplog.records


def test_default_pipeline_event_log_path_uses_temp_when_home_is_unusable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unusable home candidate does not hide writable temporary storage."""
    user_home = tmp_path / "user-home"
    host_temp = tmp_path / "host-temp"
    user_home.mkdir()
    host_temp.mkdir()
    (user_home / ".hephaestus-diagnostics").write_text("occupied", encoding="utf-8")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    path = loop_runner._pipeline_event_log_path(tmp_path / "projects", ["repo-a"])

    assert path is not None
    assert path.parent == (
        host_temp / f"hephaestus-{os.geteuid()}" / ".hephaestus-diagnostics" / "projects"
    )


def test_default_pipeline_event_log_path_uses_home_when_temp_lookup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed temporary lookup does not hide a safe home directory."""
    user_home = tmp_path / "user-home"
    user_home.mkdir()

    def unavailable_temp() -> str:
        raise OSError("temporary-directory lookup failed")

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user_home))
    monkeypatch.setattr(tempfile, "gettempdir", unavailable_temp)

    with caplog.at_level(logging.WARNING, logger=loop_runner.LOG.name):
        path = loop_runner._pipeline_event_log_path(tmp_path / "projects", ["repo-a"])

    assert path is not None
    assert path.parent == user_home / ".hephaestus-diagnostics" / "projects"
    assert not caplog.records


def test_default_pipeline_event_log_path_rejects_hostile_temp_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A public precreated user namespace cannot receive diagnostic files."""
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    namespace = host_temp / f"hephaestus-{os.geteuid()}"
    namespace.mkdir(parents=True, mode=0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    with caplog.at_level(logging.WARNING, logger=loop_runner.LOG.name):
        path = loop_runner._pipeline_event_log_path(projects_dir, ["repo-a"])

    assert path is None
    assert stat.S_IMODE(namespace.lstat().st_mode) == 0o755
    assert not (namespace / ".hephaestus-diagnostics").exists()
    assert any("event logging is disabled" in record.message for record in caplog.records)


def test_default_pipeline_event_log_path_rejects_symlinked_temp_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A planted user-namespace symlink cannot redirect diagnostics."""
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    target = tmp_path / "foreign-target"
    namespace = host_temp / f"hephaestus-{os.geteuid()}"
    host_temp.mkdir()
    target.mkdir(mode=0o700)
    namespace.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    path = loop_runner._pipeline_event_log_path(projects_dir, ["repo-a"])

    assert path is None
    assert namespace.is_symlink()
    assert not (target / ".hephaestus-diagnostics").exists()


def test_default_pipeline_event_log_path_rejects_wrong_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A namespace not owned by the effective user cannot hold diagnostics."""
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    namespace = host_temp / f"hephaestus-{os.geteuid()}"
    namespace.mkdir(parents=True, mode=0o700)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))
    monkeypatch.setattr(event_log_io, "_effective_uid", lambda: os.geteuid() + 1)

    path = loop_runner._pipeline_event_log_path(projects_dir, ["repo-a"])

    assert path is None
    assert namespace.lstat().st_uid == os.geteuid()
    assert not (namespace / ".hephaestus-diagnostics").exists()


def test_default_pipeline_event_log_path_creates_private_temp_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All event-log directories below shared temporary storage are private."""
    projects_dir = tmp_path / "projects"
    host_temp = tmp_path / "host-temp"
    host_temp.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: projects_dir))
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(host_temp))

    path = loop_runner._pipeline_event_log_path(projects_dir, ["repo-a"])

    namespace = host_temp / f"hephaestus-{os.geteuid()}"
    assert path is not None
    assert path.parent == namespace / ".hephaestus-diagnostics" / "projects"
    for directory in (namespace, namespace / ".hephaestus-diagnostics", path.parent):
        status = directory.lstat()
        assert stat.S_ISDIR(status.st_mode)
        assert stat.S_IMODE(status.st_mode) == 0o700
        assert status.st_uid == os.geteuid()


def test_default_pipeline_event_log_path_rejects_before_directory_setup_without_nofollow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing descriptor support disables diagnostics before path creation."""
    user_home = tmp_path / "user-home"
    user_home.mkdir()

    def unexpected_home_lookup(cls: type[Path]) -> Path:
        del cls
        raise AssertionError("capability admission must precede path lookup")

    monkeypatch.setattr(Path, "home", classmethod(unexpected_home_lookup))
    monkeypatch.delattr(os, "O_NOFOLLOW")

    path = loop_runner._pipeline_event_log_path(tmp_path / "projects", ["repo-a"])

    assert path is None
    assert not (user_home / ".hephaestus-diagnostics").exists()


def test_build_pipeline_config_maps_planning_stages_to_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """A planning-only top-level run must stop after plan_review."""
    loop_runner.main(["--issues", "11", "--stages", "planning,plan_review"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PLANNING, StageName.PLAN_REVIEW})


def test_build_pipeline_config_maps_implementation_stages_to_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """The implementation scope includes PR review and merge wait."""
    loop_runner.main(["--issues", "11", "--stages", "implementation,pr_review,merge_wait"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset(
        {StageName.IMPLEMENTATION, StageName.PR_REVIEW, StageName.MERGE_WAIT}
    )


def test_build_pipeline_config_maps_review_and_merge_stages_to_scope(
    dispatch: dict[str, MagicMock],
) -> None:
    """The selected stages include PR review and merge wait."""
    loop_runner.main(["--issues", "11", "--stages", "pr_review,merge_wait"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.scope is not None
    assert config.scope.stages == frozenset({StageName.PR_REVIEW, StageName.MERGE_WAIT})


def test_build_pipeline_config_maps_merge_attempts_to_budget(
    dispatch: dict[str, MagicMock],
) -> None:
    """The merge attempt limit sets the merge_wait budget."""
    loop_runner.main(["--merge-attempts", "3"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.budget_overrides["merge"] == 3


def test_build_pipeline_config_maps_explicit_review_iterations_to_exact_caps(
    dispatch: dict[str, MagicMock],
) -> None:
    """One operator review cap governs plan and implementation review rounds."""
    loop_runner.main(["--review-iterations", "10"])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.budget_overrides == {
        "merge": 5,
        "plan_review_iter": 10,
        "pr_review_iter": 10,
        "pr_review_hard": 10,
    }


def test_build_pipeline_config_omits_review_overrides_by_default(
    dispatch: dict[str, MagicMock],
) -> None:
    """Omitting the flag preserves the routing table's existing 3/3/6 defaults."""
    loop_runner.main([])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.budget_overrides == {"merge": 5}


def test_build_pipeline_config_maps_agent_and_models(
    dispatch: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pipeline path preserves provider and model selections."""
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent, **_kwargs: "codex")

    loop_runner.main(
        [
            "--agent",
            "codex",
            "--model",
            "gpt-default",
            "--planner-model",
            "gpt-plan",
            "--reviewer-model",
            "gpt-review",
            "--implementer-model",
            "gpt-impl",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.agent == "codex"
    assert config.model == "gpt-default"
    assert config.planner_model == "gpt-plan"
    assert config.reviewer_model == "gpt-review"
    assert config.implementer_model == "gpt-impl"


def test_build_pipeline_config_keeps_per_role_inline_reasoning_effort(
    dispatch: dict[str, MagicMock], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop keeps each role effort in its model reference."""
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda agent, **_kwargs: "codex")

    loop_runner.main(
        [
            "--agent",
            "codex",
            "--model",
            "gpt-5.6",
            "--planner-model",
            "sol:high",
            "--reviewer-model",
            "terra:default",
            "--implementer-model",
            "luna:xhigh",
        ]
    )

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.planner_model == "sol:high"
    assert config.reviewer_model == "terra:default"
    assert config.implementer_model == "luna:xhigh"
    assert not hasattr(config, "planner_reasoning_effort")


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_build_pipeline_config_preserves_agent_model_default(
    dispatch: dict[str, MagicMock],
    monkeypatch: pytest.MonkeyPatch,
    agent: str,
) -> None:
    """Direct agents do not receive an implicit Claude role model."""
    monkeypatch.setattr(loop_runner, "resolve_agent", lambda selected, **_kwargs: agent)

    loop_runner.main(["--agent", agent])

    (config,) = dispatch["run_pipeline"].call_args.args
    assert config.model == ""
    assert config.planner_model == ""
    assert config.reviewer_model == ""
    assert config.implementer_model == ""
    assert config.fallback_model == ""


@pytest.mark.parametrize(
    ("role", "model", "expected"),
    [
        ("planner", "sol:xhigh", "sol:xhigh"),
        ("implementer", "terra:high", "terra:high"),
        ("reviewer", "terra:default", "terra:default"),
    ],
)
def test_stage_model_propagates_inline_reasoning_effort(
    role: str, model: str, expected: str
) -> None:
    """Every pipeline role transports its model reference to the runtime."""
    config = SimpleNamespace(
        agent="codex",
        model="",
        planner_model=model if role == "planner" else "",
        implementer_model=model if role == "implementer" else "",
        reviewer_model=model if role == "reviewer" else "",
    )

    context = cast(StageContext, SimpleNamespace(config=config))
    assert stage_model(context, role, lambda: model) == expected


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-4-6:future-effort", ":provider-default"],
)
def test_stage_model_preserves_claude_selection_until_invocation(model: str) -> None:
    """The pipeline keeps the compact selection until Claude starts."""
    config = SimpleNamespace(
        agent="claude",
        model="",
        reviewer_model=model,
    )

    context = cast(StageContext, SimpleNamespace(config=config))
    assert stage_model(context, "reviewer", lambda: "fallback") == model


def test_stage_model_uses_the_explicit_pi_alias() -> None:
    """Pi jobs use the explicit role model rather than an ambient alias."""
    config = SimpleNamespace(
        agent="pi",
        model="",
        reviewer_model="operator-local-pi-alias:default",
    )

    context = cast(StageContext, SimpleNamespace(config=config))

    selection = parse_model_selection(stage_model(context, "reviewer", lambda: "claude-sonnet-4-6"))

    assert selection.model == "operator-local-pi-alias"
    assert selection.reasoning_effort == "default"


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_stage_model_uses_agent_config_default_when_model_is_omitted(agent: str) -> None:
    """OpenCode and Pi do not receive a Claude role default."""
    config = SimpleNamespace(
        agent=agent,
        model="",
        reviewer_model="",
    )

    context = cast(StageContext, SimpleNamespace(config=config))

    assert stage_model(context, "reviewer", lambda: "claude-sonnet-4-6") == ""


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_stage_model_adds_reasoning_for_supported_direct_agents(agent: str) -> None:
    """An inline free-form effort applies to a direct-agent model."""
    config = SimpleNamespace(
        agent=agent,
        model="",
        reviewer_model="k2-horizon-7:future-effort",
    )

    context = cast(StageContext, SimpleNamespace(config=config))

    assert stage_model(context, "reviewer", lambda: "fallback") == ("k2-horizon-7:future-effort")


@pytest.mark.parametrize("model", ["terra:default", "gpt-5.6-terra:default"])
def test_stage_model_preserves_existing_codex_reasoning_selector(model: str) -> None:
    """The model reference is the single reasoning-selection source."""
    config = SimpleNamespace(
        agent="codex",
        model="",
        reviewer_model=model,
    )

    context = cast(StageContext, SimpleNamespace(config=config))
    assert stage_model(context, "reviewer", lambda: "fallback") == model


def test_phase_timeout_help_documents_agent_job_scope() -> None:
    """The --phase-timeout help names the per-agent-job semantic."""
    parser = loop_runner.build_parser()
    action = next(a for a in parser._actions if "--phase-timeout" in a.option_strings)

    assert "agent job" in (action.help or "").lower()
