"""Authenticated GitHub contract tests (opt-in; see docs/contract-testing.md)."""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

import hephaestus.github.client as github_client
from hephaestus.github.client import gh_call

pytestmark = [pytest.mark.integration, pytest.mark.contract]


def test_rate_limit_envelope(gh_authenticated: None) -> None:
    """``gh api rate_limit`` returns the envelope consumed by the client."""
    result = gh_call(["api", "rate_limit"])
    payload = json.loads(result.stdout)
    core = payload["resources"]["core"]
    assert core["limit"] > 0
    assert {"remaining", "reset"} <= core.keys()


def test_repo_view_json_fields(contract_repo: str) -> None:
    """``gh repo view --json`` serves the fields automation queries."""
    result = gh_call(["repo", "view", contract_repo, "--json", "nameWithOwner,defaultBranchRef"])
    payload = json.loads(result.stdout)
    assert payload["nameWithOwner"].lower() == contract_repo.lower()
    assert payload["defaultBranchRef"]["name"]


def test_issue_list_json_fields(contract_repo: str) -> None:
    """``gh issue list --json`` exposes the pipeline's parsed fields."""
    result = gh_call(
        [
            "issue",
            "list",
            "-R",
            contract_repo,
            "--limit",
            "1",
            "--json",
            "number,title,labels",
            "--state",
            "all",
        ]
    )
    issues = json.loads(result.stdout)
    assert isinstance(issues, list)
    if issues:
        assert {"number", "title", "labels"} <= issues[0].keys()


def test_missing_endpoint_raises_promptly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic 404 bypasses the retry budget after one CLI attempt."""
    calls: list[list[str]] = []
    endpoint = f"repos/missing-{uuid.uuid4().hex[:12]}"

    def missing_endpoint(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        raise subprocess.CalledProcessError(
            1,
            command,
            output="",
            stderr="gh: Not Found (HTTP 404)",
        )

    # Keep this regression assertion local: a generated real endpoint makes the
    # outcome depend on GitHub, while this seam verifies gh_call's retry contract.
    github_client._GH_BREAKER.reset()
    monkeypatch.setattr(github_client, "run_subprocess", missing_endpoint)
    try:
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            gh_call(["api", endpoint], max_retries=3)
    finally:
        github_client._GH_BREAKER.reset()

    assert excinfo.value.stderr == "gh: Not Found (HTTP 404)"
    assert calls == [["gh", "api", endpoint]]
