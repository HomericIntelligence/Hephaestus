"""Regression tests for the direct CLIs' explicit trusted ``gh`` root."""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import ANY, call, patch

import pytest

from hephaestus.automation import implementer, planner, pr_reviewer
from hephaestus.automation.pipeline.jobs import JobResult
from hephaestus.automation.pipeline.worker_pool import WorkerPool

DIRECT_CLIS: tuple[tuple[ModuleType, str], ...] = (
    (planner, "hephaestus-plan-issues"),
    (implementer, "hephaestus-implement-issues"),
    (pr_reviewer, "hephaestus-review-prs"),
)


def _make_executable_gh(root: Path) -> Path:
    """Create the executable admitted by the explicit-root contract."""
    executable = root / "bin" / "gh"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o755)
    return executable


def _capture_pipeline_config(
    module: ModuleType,
    program: str,
    argv: list[str],
    repo_root: Path,
) -> Any:
    """Run one direct wrapper without external calls and return its config."""
    captured: dict[str, Any] = {}

    def _fake_run_pipeline(config: Any) -> int:
        captured["config"] = config
        return 0

    with ExitStack() as stack:
        stack.enter_context(patch.object(sys, "argv", [program, *argv]))
        stack.enter_context(patch.object(module, "_resolve_repo", return_value=("acme", "widget")))
        stack.enter_context(patch.object(module, "resolve_agent", return_value="claude"))
        stack.enter_context(
            patch(
                "hephaestus.automation.pipeline.coordinator.run_pipeline",
                side_effect=_fake_run_pipeline,
            )
        )
        if module is implementer:
            stack.enter_context(patch.object(implementer, "get_repo_root", return_value=repo_root))
        assert module.main() == 0

    return captured["config"]


@pytest.mark.parametrize(("module", "program"), DIRECT_CLIS)
def test_direct_cli_propagates_explicit_trusted_gh_root(
    module: ModuleType,
    program: str,
    tmp_path: Path,
) -> None:
    """Each direct wrapper passes the validated, resolved root into the pipeline."""
    trusted_root = tmp_path / "trusted-gh"
    _make_executable_gh(trusted_root)

    config = _capture_pipeline_config(
        module,
        program,
        ["--issues", "17", "--dry-run", "--gh-extra-path-root", str(trusted_root)],
        tmp_path,
    )

    assert config.gh_extra_path_root == trusted_root.resolve()


def test_direct_issue_sync_uses_explicit_gh_root_when_fixed_candidates_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A direct issue run uses the admitted ``ROOT/bin/gh`` during checkout sync."""
    from hephaestus.automation.pipeline import worker_pool as worker_pool_module

    trusted_root = tmp_path / "trusted-gh"
    executable = _make_executable_gh(trusted_root)
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(worker_pool_module, "_TRUSTED_GH_CANDIDATES", ())
    gh_api_call: Any = None

    def _run_direct_pipeline(config: Any) -> int:
        nonlocal gh_api_call
        pool = WorkerPool(
            size=1,
            shutdown=threading.Event(),
            completion_q=queue.Queue(),
            gh_extra_path_root=config.gh_extra_path_root,
        )
        try:
            with (
                patch("hephaestus.automation.git_utils.run") as mock_run,
                patch.object(
                    pool,
                    "_fast_forward_checkout",
                    return_value=JobResult(ok=True),
                ),
            ):
                mock_run.side_effect = [
                    subprocess.CompletedProcess(
                        [], 0, stdout="https://github.com/acme/widget.git\n"
                    ),
                    subprocess.CompletedProcess([], 0, stdout=""),
                    subprocess.CompletedProcess([], 0, stdout="main\n"),
                    subprocess.CompletedProcess([], 0, stdout="main\n"),
                ]
                result = pool._sync_checkout_locked(
                    checkout=checkout,
                    expected_repo="acme/widget",
                    timeout_s=120,
                )
                gh_api_call = mock_run.call_args_list[3]
        finally:
            pool.shutdown()

        assert result.ok is True
        return 0

    with (
        patch.object(
            sys,
            "argv",
            [
                "hephaestus-implement-issues",
                "--issues",
                "17",
                "--gh-extra-path-root",
                str(trusted_root),
            ],
        ),
        patch.object(implementer, "_resolve_repo", return_value=("acme", "widget")),
        patch.object(implementer, "resolve_agent", return_value="claude"),
        patch.object(implementer, "get_repo_root", return_value=tmp_path),
        patch(
            "hephaestus.automation.pipeline.coordinator.run_pipeline",
            side_effect=_run_direct_pipeline,
        ),
    ):
        assert implementer.main() == 0

    assert gh_api_call == call(
        [
            str(executable.resolve()),
            "api",
            "repos/acme/widget",
            "--jq",
            ".default_branch",
        ],
        cwd=checkout,
        timeout=120,
        env=ANY,
    )


def _relative_root(tmp_path: Path) -> str:
    return "relative-gh-root"


def _root_without_gh(tmp_path: Path) -> str:
    root = tmp_path / "missing-gh"
    root.mkdir()
    return str(root)


def _root_with_symlink_escape(tmp_path: Path) -> str:
    root = tmp_path / "escaping-gh"
    outside = tmp_path / "outside-gh"
    _make_executable_gh(root)
    outside.write_text("#!/bin/sh\n")
    outside.chmod(0o755)
    (root / "bin" / "gh").unlink()
    (root / "bin" / "gh").symlink_to(outside)
    return str(root)


@pytest.mark.parametrize(("module", "program"), DIRECT_CLIS)
@pytest.mark.parametrize(
    "invalid_root",
    (_relative_root, _root_without_gh, _root_with_symlink_escape),
    ids=("relative", "missing-executable", "symlink-escape"),
)
def test_direct_cli_rejects_untrusted_gh_root_during_parsing(
    module: ModuleType,
    program: str,
    invalid_root: Any,
    tmp_path: Path,
) -> None:
    """Direct wrappers reject the same root classes as the full loop."""
    with pytest.raises(SystemExit) as excinfo:
        module._parse_args(["--issues", "17", "--gh-extra-path-root", invalid_root(tmp_path)])

    assert excinfo.value.code == 2
