"""Preflight fixtures for the opt-in contract lane (issue #2146).

Lane gating (``HEPHAESTUS_CONTRACT_TESTS``) lives in ``tests/conftest.py``;
these fixtures skip individual tests whose external prerequisite is absent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hephaestus.utils.helpers import run_subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def gh_authenticated() -> None:
    """Skip unless a real, authenticated ``gh`` CLI is available."""
    try:
        result = run_subprocess(
            ["gh", "auth", "status"],
            check=False,
            timeout=30,
            log_on_error=False,
        )
    except FileNotFoundError:
        pytest.skip("gh CLI not installed")
    if result.returncode != 0:
        pytest.skip("gh CLI is not authenticated (gh auth status failed)")


@pytest.fixture(scope="session")
def contract_repo(gh_authenticated: None) -> str:
    """Return the explicitly selected repository slug for contract calls.

    ``HEPHAESTUS_CONTRACT_REPO`` takes precedence. Otherwise the repository is
    resolved once from the checked-out root with an explicit ``cwd`` so the
    lane cannot target an ambient working directory's repository.
    """
    slug = os.environ.get("HEPHAESTUS_CONTRACT_REPO", "").strip()
    if slug:
        return slug

    result = run_subprocess(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    return result.stdout.strip()


@pytest.fixture()
def agent_lane_enabled() -> None:
    """Skip the token-costly agent lane unless explicitly enabled."""
    if os.environ.get("HEPHAESTUS_CONTRACT_AGENT") != "1":
        pytest.skip("agent contract lane spends model tokens; set HEPHAESTUS_CONTRACT_AGENT=1")
