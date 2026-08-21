"""Regression tests for suite-wide stubs maintained by ``tests/conftest.py``."""

import inspect
from pathlib import Path
from typing import Protocol, cast

from hephaestus.agents import runtime


class _Resolver(Protocol):
    """Cross-lane resolver contract expected by automation call sites."""

    def __call__(
        self,
        agent: str | None,
        *,
        cwd: Path | None = None,
        disable_pi_automation: bool = False,
        auth_status_timeout: int = 10,
    ) -> str: ...


class _Authenticator(Protocol):
    """Cross-lane authentication probe contract expected by runtime callers."""

    def __call__(
        self,
        agent: str,
        *,
        auth_status_timeout: int = 10,
    ) -> bool: ...


def test_authenticated_agent_stub_accepts_timeout_keyword() -> None:
    """The auth stub remains substitutable for timeout-aware production calls."""
    authenticate = cast(_Authenticator, runtime.is_agent_authenticated)
    assert authenticate("claude", auth_status_timeout=1) is True


def test_resolve_agent_stub_accepts_runtime_policy_keywords(tmp_path: Path) -> None:
    """The authenticated-agent stub remains substitutable for the production API."""
    parameters = inspect.signature(runtime.resolve_agent).parameters
    assert parameters["disable_pi_automation"].default is False
    assert parameters["auth_status_timeout"].default == 10
    resolve_agent = cast(_Resolver, runtime.resolve_agent)

    assert (
        resolve_agent(
            None,
            cwd=tmp_path,
            disable_pi_automation=False,
            auth_status_timeout=10,
        )
        == "claude"
    )
    assert (
        resolve_agent(
            "codex",
            disable_pi_automation=True,
            auth_status_timeout=1,
        )
        == "codex"
    )
