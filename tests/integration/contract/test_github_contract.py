"""Authenticated GitHub contract tests (opt-in; see docs/contract-testing.md).

Every call routes through the production ``gh_call`` chokepoint (never a bare
``subprocess.run(["gh", ...])``, which would bypass ``_GH_BREAKER``), and every
call is read-only, so the lane cannot mutate the target repository.
"""

from __future__ import annotations

import json
import subprocess
import uuid

import pytest

from hephaestus.github.client import gh_call

pytestmark = [pytest.mark.integration, pytest.mark.contract]


def test_rate_limit_envelope(gh_authenticated: None) -> None:
    """`gh api rate_limit` authenticates and returns the envelope client.py relies on."""
    result = gh_call(["api", "rate_limit"])
    payload = json.loads(result.stdout)
    core = payload["resources"]["core"]
    assert core["limit"] > 0
    assert "remaining" in core
    assert "reset" in core


def test_repo_view_json_fields(contract_repo: str) -> None:
    """`repo view --json` serves the fields automation queries."""
    result = gh_call(["repo", "view", contract_repo, "--json", "nameWithOwner,defaultBranchRef"])
    payload = json.loads(result.stdout)
    assert payload["nameWithOwner"].lower() == contract_repo.lower()
    assert payload["defaultBranchRef"]["name"]


def test_issue_list_json_fields(contract_repo: str) -> None:
    """`issue list --json` field names match what the pipeline parses."""
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


def test_missing_endpoint_raises_promptly(contract_repo: str) -> None:
    """A 404 is non-transient: gh_call raises without a retry storm."""
    bogus = f"repos/{contract_repo}/definitely-missing-{uuid.uuid4().hex[:12]}"
    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        gh_call(["api", bogus], max_retries=1)
    assert "404" in (excinfo.value.stderr or "")
