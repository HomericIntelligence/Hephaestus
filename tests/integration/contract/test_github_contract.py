"""Authenticated GitHub contract tests (opt-in; see docs/contract-testing.md)."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

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


def test_missing_endpoint_raises_promptly(
    gh_authenticated: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A deterministic 404 bypasses the retry budget after one CLI attempt."""
    endpoint = f"repos/missing-{uuid.uuid4().hex[:12]}"
    real_gh = shutil.which("gh")
    assert real_gh is not None
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    invocation_log = tmp_path / "gh-invocations"
    wrapper = wrapper_dir / "gh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' call >> {shlex.quote(str(invocation_log))}\n"
        f'exec {shlex.quote(real_gh)} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}")

    github_client._GH_BREAKER.reset()
    try:
        with pytest.raises(subprocess.CalledProcessError) as excinfo:
            gh_call(["api", endpoint], max_retries=3)
    finally:
        github_client._GH_BREAKER.reset()

    assert excinfo.value.stderr.strip() == "gh: Not Found (HTTP 404)"
    assert invocation_log.read_text(encoding="utf-8").splitlines() == ["call"]
