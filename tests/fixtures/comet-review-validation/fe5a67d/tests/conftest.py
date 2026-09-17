"""Shared test environment."""

import grp
import os
from types import SimpleNamespace

import pytest

import comet.service_paths as service_paths
from tests.deployment_context_helpers import TEST_ENGINE_API_KEY

pytest_plugins = ["tests.release_effect_guard"]


@pytest.fixture(autouse=True)
def service_runner_identity(monkeypatch):
    """Give test-owned service files the local service identity."""
    original_getpwnam = service_paths.pwd.getpwnam
    local_runner = SimpleNamespace(
        pw_name="comet.runner",
        pw_uid=os.geteuid(),
        pw_gid=os.getegid(),
    )

    def getpwnam(principal):
        if principal == "comet.runner":
            return local_runner
        return original_getpwnam(principal)

    monkeypatch.setattr(service_paths.pwd, "getpwnam", getpwnam)


@pytest.fixture(autouse=True)
def administrator_group_identity(monkeypatch):
    """Resolve the administrator group to the fixture filesystem group."""
    original_getgrnam = grp.getgrnam

    def getgrnam(name):
        if name == "comet":
            return SimpleNamespace(gr_name=name, gr_gid=os.getegid(), gr_mem=[])
        return original_getgrnam(name)

    monkeypatch.setattr(grp, "getgrnam", getgrnam)


@pytest.fixture(autouse=True)
def engine_api_key(monkeypatch):
    """Model the internal key required by every production job."""
    monkeypatch.setenv("COMET_ENGINE_API_KEY", TEST_ENGINE_API_KEY)
    monkeypatch.setenv("COMET_ENGINE_API_KEY_CLASS", "local-deployment")
    monkeypatch.setenv("COMET_ENGINE_API_KEY_CLUSTER", "m2")
    monkeypatch.setenv("COMET_ENGINE_API_KEY_DEPLOYMENT", "m2-development")
    monkeypatch.setenv("COMET_ENGINE_API_KEY_ROLE", "comet.runner")
    monkeypatch.setenv("COMET_API_KEY_CLASS", "ordinary-user")
    monkeypatch.setenv("COMET_API_KEY_CLUSTER", "m2")


@pytest.fixture
def root_only_environment(monkeypatch):
    """Remove ambient deployment selectors for an isolated test."""
    monkeypatch.delenv("COMET_CLUSTER", raising=False)
    monkeypatch.delenv("COMET_DEPLOYMENT_CONTEXT", raising=False)
    monkeypatch.delenv("COMET_ROOT", raising=False)
