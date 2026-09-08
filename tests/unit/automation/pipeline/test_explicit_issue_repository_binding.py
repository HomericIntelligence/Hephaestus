"""Tests for repository identity through CLI admission and the coordinator."""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from hephaestus.automation import github_api, loop_runner
from hephaestus.automation.pipeline import coordinator as coordinator_module
from hephaestus.automation.pipeline.coordinator import Coordinator, PipelineConfig
from tests.unit.automation.pipeline.conftest import FakeWorkerPool
from tests.unit.automation.pipeline.stages.conftest import FakeStageGitHub


@contextmanager
def _restore_logging() -> Iterator[None]:
    """Remove CLI log handlers before pytest closes its capture streams."""
    logger = logging.getLogger()
    handlers = logger.handlers[:]
    level = logger.level
    try:
        yield
    finally:
        for handler in logger.handlers[:]:
            if handler not in handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.handlers[:] = handlers
        logger.setLevel(level)


def _coordinator_with_fake_pool(config: PipelineConfig, **kwargs: Any) -> Coordinator:
    """Use Coordinator without external agent processes."""
    return Coordinator(config, pool=FakeWorkerPool(), install_signals=False, **kwargs)


def _run_selected_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    same_checkout: bool = False,
    read_failure: bool = False,
) -> tuple[int, dict[str, Any], set[tuple[str, str, int]]]:
    """Run loop_runner.main and Coordinator with controlled external dependencies."""
    ambient = ("checkout-owner", "checkout")
    selected = ambient if same_checkout else ("target-owner", "target")
    checkout = tmp_path / ambient[1]
    checkout.mkdir()
    (tmp_path / selected[1]).mkdir(exist_ok=True)
    monkeypatch.chdir(checkout)
    monkeypatch.setattr(github_api, "_issue_state_cache", {})
    monkeypatch.setattr(github_api, "get_repo_info", lambda: ambient)
    monkeypatch.setattr(loop_runner, "_detect_cwd_repo", lambda **kwargs: ambient)
    monkeypatch.setattr(loop_runner, "DEFAULT_STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(loop_runner, "_preflight_token_scopes", lambda *args, **kwargs: None)
    monkeypatch.setattr("hephaestus.utils.terminal.install_sigtstp_only", lambda: None)
    monkeypatch.setattr(
        "hephaestus.automation.pipeline_github_transport.rate_limit_remaining",
        lambda **kwargs: (5000, 0),
    )
    classified: set[tuple[str, str, int]] = set()

    def state(repo: tuple[str, str], number: int) -> str:
        if read_failure and repo == selected:
            raise RuntimeError("NOT_FOUND: target issue is unavailable")
        if repo == selected:
            return "OPEN" if number == 27 else "CLOSED"
        assert repo == ambient
        return "CLOSED" if number == 27 else "OPEN"

    def transport(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        payload: dict[str, Any]
        if argv[:2] == ["api", "graphql"]:
            variables = dict(value.split("=", 1) for value in argv if "=" in value)
            repo = (variables["owner"], variables["name"])
            repository: dict[str, Any] = {"owner": {"login": repo[0]}, "name": repo[1]}
            try:
                for key, number in variables.items():
                    if key.startswith("n") and key[1:].isdigit():
                        repository[f"issue{key[1:]}"] = {
                            "number": int(number),
                            "state": state(repo, int(number)),
                        }
            except RuntimeError as error:
                raise subprocess.CalledProcessError(
                    1, argv, stderr="HTTP 404: NOT_FOUND"
                ) from error
            payload = {"data": {"repository": repository}}
        else:
            assert argv[:2] == ["issue", "view"]
            owner, name = argv[argv.index("--repo") + 1].split("/") if "--repo" in argv else ambient
            issue_number = int(argv[2])
            payload = {"number": issue_number, "state": state((owner, name), issue_number)}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    class RepositoryGitHub(FakeStageGitHub):
        """Return issue facts only for the accessor's repository."""

        def __init__(self, org: str, *, repo: str, **kwargs: Any) -> None:
            super().__init__(labels=["state:plan-go"])
            self.repository = (org, repo)

        def gh_issue_json(self, issue_number: int) -> dict[str, Any]:
            result = super().gh_issue_json(issue_number)
            result["state"] = state(self.repository, issue_number)
            return result

        def find_pr_for_issue(self, issue_number: int) -> int | None:
            classified.add((*self.repository, issue_number))
            return None

    monkeypatch.setattr(github_api, "_gh_call", transport)
    monkeypatch.setattr("hephaestus.automation.pipeline_github.PipelineGitHub", RepositoryGitHub)
    monkeypatch.setattr(coordinator_module, "Coordinator", _coordinator_with_fake_pool)

    with _restore_logging():
        result = loop_runner.main(
            [
                "--org",
                selected[0],
                "--repos",
                selected[1],
                "--issues",
                "27" if read_failure else "27,28",
                "--projects-dir",
                str(tmp_path),
                "--phases",
                "plan",
                "--loops",
                "1",
                "--max-workers",
                "1",
                "--agent",
                "claude",
                "--no-advise",
                "--no-learn",
                "--json",
            ]
        )
    return result, json.loads(capsys.readouterr().out), classified


def test_cli_selected_repository_ignores_checkout_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Use the explicit CLI repository when issue numbers have different states."""
    result, summary, classified = _run_selected_repository(tmp_path, monkeypatch, capsys)
    assert result == 0
    assert classified == {("target-owner", "target", 27)}
    assert summary["dispositions"] == {"pass": 1}
    assert summary["agent_jobs"] == 0


def test_cli_unverifiable_repository_state_returns_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A target read failure that continues must return exit code 1."""
    result, summary, _ = _run_selected_repository(tmp_path, monkeypatch, capsys, read_failure=True)
    assert result == 1
    assert summary["dispositions"] == {"fail": 1}
    assert summary["agent_jobs"] == 0


def test_cli_current_checkout_scope_is_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Keep a CLI scope that selects the current checkout."""
    result, summary, classified = _run_selected_repository(
        tmp_path, monkeypatch, capsys, same_checkout=True
    )
    assert result == 0
    assert classified == {("checkout-owner", "checkout", 27)}
    assert summary["dispositions"] == {"pass": 1}
    assert summary["agent_jobs"] == 0
