#!/usr/bin/env python3
"""Shared test fixtures for Hephaestus tests."""

import contextlib
import json

import pytest
import yaml


@pytest.fixture(autouse=True)
def _agents_authenticated_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub agent install+auth resolution suite-wide (#1175).

    A real agent backend (``claude``/``codex``/``pi``) is deliberately NOT
    installed or authenticated in CI/CD — we do not want tests to set up agent
    accounts or spend tokens. But ``resolve_agent`` refuses a selection unless a
    CLI is installed on PATH AND reports authenticated, so every test that
    dispatches a named agent (mocking the actual run_* call) would otherwise hit
    ``RuntimeError: ... not installed / not authenticated`` on an agent-free
    runner. Stub both halves suite-wide:

    * ``is_agent_authenticated`` -> always True.
    * ``resolve_agent`` -> return the requested backend, or ``"claude"`` when the
      caller passed no ``--agent`` — with no PATH probe. This makes the whole
      suite independent of whether an agent binary exists on the runner.

    EXCEPTION: ``tests/unit/agents/test_runtime.py`` tests the resolution
    machinery itself, driving real install+auth detection via patched
    ``shutil.which``/``subprocess.run``. Stubbing it there would short-circuit the
    logic under test, so skip the stub for that module. Other tests that need the
    unauthenticated/uninstalled path can override with their own monkeypatch
    (last-writer-wins).
    """
    if request.module.__name__.endswith("agents.test_runtime"):
        return
    monkeypatch.setattr(
        "hephaestus.agents.runtime.is_agent_authenticated",
        lambda _agent: True,
    )

    def _stub_resolve_agent(agent: str | None) -> str:
        return agent if agent is not None else "claude"

    # Patch at the runtime module and at every automation module that imported
    # ``resolve_agent`` by value (``from ...runtime import resolve_agent``).
    monkeypatch.setattr("hephaestus.agents.runtime.resolve_agent", _stub_resolve_agent)
    for mod in ("implementer", "loop_runner", "planner", "pr_reviewer", "audit_reviewer"):
        target = f"hephaestus.automation.{mod}.resolve_agent"
        # A module that does not import resolve_agent by value has nothing to patch.
        with contextlib.suppress(AttributeError):
            monkeypatch.setattr(target, _stub_resolve_agent)


@pytest.fixture
def tmp_config_yaml(tmp_path):
    """Create a temporary YAML config file."""
    config = {
        "database": {
            "host": "localhost",
            "port": 5432,
            "name": "test_db",
        },
        "api": {
            "timeout": 30,
            "retries": 3,
        },
        "debug": True,
    }
    config_file = tmp_path / "config.yaml"
    config_file.write_text(yaml.dump(config))
    return config_file


@pytest.fixture
def tmp_config_json(tmp_path):
    """Create a temporary JSON config file."""
    config = {
        "app": {"name": "test", "version": "1.0"},
        "logging": {"level": "INFO"},
    }
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(config, indent=2))
    return config_file


@pytest.fixture
def tmp_text_file(tmp_path):
    """Create a temporary text file with sample content."""
    content = "Hello, World!\nLine 2\nLine 3\n"
    text_file = tmp_path / "sample.txt"
    text_file.write_text(content)
    return text_file


@pytest.fixture
def tmp_json_data_file(tmp_path):
    """Create a temporary JSON data file."""
    data = {"key": "value", "numbers": [1, 2, 3], "nested": {"a": 1}}
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps(data))
    return data_file


@pytest.fixture
def tmp_yaml_data_file(tmp_path):
    """Create a temporary YAML data file."""
    data = {"items": ["a", "b", "c"], "count": 3}
    data_file = tmp_path / "data.yaml"
    data_file.write_text(yaml.dump(data))
    return data_file


@pytest.fixture
def mock_git_repo(tmp_path):
    """Create a minimal fake git repository structure."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return tmp_path


@pytest.fixture
def sample_config():
    """Return a sample in-memory configuration dictionary."""
    return {
        "database": {
            "host": "localhost",
            "port": 5432,
            "credentials": {
                "user": "admin",
                "password": "secret",
            },
        },
        "feature_flags": {
            "new_ui": True,
            "beta_api": False,
        },
    }
