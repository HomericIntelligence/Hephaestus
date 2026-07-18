"""Preflight fixtures for the opt-in contract lane (issue #2146).

Lane gating (``HEPHAESTUS_CONTRACT_TESTS``) lives in ``tests/conftest.py``;
these fixtures skip individual tests whose external prerequisite is absent so a
partially-provisioned environment produces clean skips rather than failures.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hephaestus.utils.helpers import run_subprocess

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="session")
def gh_authenticated() -> None:
    """Skip unless a real, authenticated ``gh`` CLI is available.

    Uses :func:`run_subprocess` directly rather than ``gh_call`` so that an
    unauthenticated environment produces a clean skip without tripping the
    shared ``_GH_BREAKER`` circuit breaker in ``hephaestus.github.client``.
    """
    try:
        result = run_subprocess(["gh", "auth", "status"], check=False, timeout=30)
    except FileNotFoundError:
        pytest.skip("gh CLI not installed")
    if result.returncode != 0:
        pytest.skip("gh CLI is not authenticated (gh auth status failed)")


@pytest.fixture(scope="session")
def contract_repo(gh_authenticated: None) -> str:
    """Target repo slug: ``HEPHAESTUS_CONTRACT_REPO``, else resolved from ``REPO_ROOT``.

    ``cwd`` is pinned explicitly — never ambient — so the lane cannot misroute
    to whatever repository the runner happens to be sitting in (this repo has a
    documented wrong-repo-404 breaker-cascade failure mode from ambient CWD
    resolution).
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
    """Skip the agent lane unless double-opted-in — it spends real model tokens."""
    if os.environ.get("HEPHAESTUS_CONTRACT_AGENT") != "1":
        pytest.skip("agent contract lane spends model tokens; set HEPHAESTUS_CONTRACT_AGENT=1")
