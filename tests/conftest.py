#!/usr/bin/env python3
"""Shared test fixtures for ProjectHephaestus tests."""

import json

import pytest
import yaml


@pytest.fixture(autouse=True)
def _agents_authenticated_by_default(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stub the agent install+auth pre-flight (#1175) to pass by default.

    ``resolve_agent`` now refuses a ``--agent claude|codex|pi`` selection unless the
    CLI is installed AND reports authenticated (#1175) — a real guard so an
    unauthenticated backend cannot silently produce empty output. But these CLIs
    is installed in CI, so every test that dispatches a named agent through
    automation, fleet-sync, tidy, etc. (mocking the actual run_* call) would
    otherwise hit ``RuntimeError: Agent '...' is not installed``. Default the
    pre-flight to "authenticated" suite-wide so those mocked-dispatch tests pass.

    EXCEPTION: ``tests/unit/agents/test_runtime.py`` tests the pre-flight machinery
    itself (``resolve_agent``/``is_agent_authenticated``), driving the real
    install+auth detection via patched ``shutil.which``/``subprocess.run``. Stubbing
    ``is_agent_authenticated`` there would short-circuit the very logic under test,
    so skip the stub for that module — it owns the real behaviour. Other tests that
    need the unauthenticated path can likewise override this with their own
    monkeypatch (last-writer-wins).
    """
    if request.module.__name__.endswith("agents.test_runtime"):
        return
    monkeypatch.setattr(
        "hephaestus.agents.runtime.is_agent_authenticated",
        lambda _agent: True,
    )


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
