"""Preflight fixtures for the opt-in contract lane (issue #2146).

Lane gating lives in ``tests/conftest.py``; these fixtures skip individual
tests whose external prerequisite is absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hephaestus.config.child_environments import build_gh_child_env
from hephaestus.utils.helpers import run_subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def gh_authenticated() -> None:
    """Skip unless a real, authenticated ``gh`` CLI is available."""
    try:
        result = run_subprocess(
            ["gh", "auth", "status"],
            env=build_gh_child_env(),
            check=False,
            timeout=30,
            log_on_error=False,
        )
    except FileNotFoundError:
        pytest.skip("gh CLI not installed")
    if result.returncode != 0:
        pytest.skip("gh CLI is not authenticated (gh auth status failed)")


@pytest.fixture(scope="session")
def contract_repo(gh_authenticated: None, contract_repo_option: str | None) -> str:
    """Return the explicitly selected repository slug for contract calls.

    The explicit CLI selection takes precedence. Otherwise the repository is
    resolved once from the checked-out root with an explicit ``cwd`` so the lane
    cannot target an ambient working directory's repository.
    """
    if contract_repo_option:
        return contract_repo_option

    result = run_subprocess(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        env=build_gh_child_env(),
        cwd=str(REPO_ROOT),
        timeout=60,
    )
    return result.stdout.strip()


@pytest.fixture()
def agent_lane_enabled(contract_agent_enabled: bool) -> None:
    """Skip the token-costly agent lane unless explicitly enabled."""
    if not contract_agent_enabled:
        pytest.skip("agent contract lane spends model tokens; pass --run-contract-agent")
