#!/usr/bin/env python3
"""Shared test fixtures for Hephaestus tests."""

import contextlib
import json
import os
from pathlib import Path

# Repository policy requires generated files to live under build/. Hypothesis
# consults this environment variable lazily when it first opens its storage,
# so set the suite-wide location before importing test modules.
os.environ["HYPOTHESIS_STORAGE_DIRECTORY"] = str(
    Path(__file__).resolve().parents[1] / "build" / ".hypothesis"
)

import pytest
import yaml


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register explicit controls for opt-in and CI-enforced test lanes."""
    group = parser.getgroup("hephaestus", "Hephaestus test controls")
    group.addoption(
        "--run-contract-tests",
        action="store_true",
        help="run authenticated external contract tests",
    )
    group.addoption(
        "--run-contract-agent",
        action="store_true",
        help="run contract tests that invoke an agent and spend model tokens",
    )
    group.addoption(
        "--contract-repo",
        metavar="OWNER/REPOSITORY",
        help="target repository for authenticated GitHub contract tests",
    )
    group.addoption(
        "--contract-model",
        default="haiku",
        metavar="MODEL",
        help="agent model for contract tests (default: haiku)",
    )
    group.addoption(
        "--require-cli",
        action="store_true",
        help="fail instead of skip when an installed console script is missing",
    )
    group.addoption(
        "--require-pi-package-smoke",
        action="store_true",
        help="require live package evidence in the nightly Pi smoke tests",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip contract-marked tests unless the explicit CLI option is enabled."""
    if config.getoption("run_contract_tests"):
        return

    skip = pytest.mark.skip(reason="contract lane is opt-in; pass --run-contract-tests to run")
    for item in items:
        if item.get_closest_marker("contract"):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def contract_agent_enabled(pytestconfig: pytest.Config) -> bool:
    """Return whether token-spending contract agent tests were requested."""
    return bool(pytestconfig.getoption("run_contract_agent"))


@pytest.fixture(scope="session")
def contract_repo_option(pytestconfig: pytest.Config) -> str | None:
    """Return the explicitly selected contract repository, if any."""
    value = pytestconfig.getoption("contract_repo")
    return str(value).strip() if value else None


@pytest.fixture(scope="session")
def contract_model(pytestconfig: pytest.Config) -> str:
    """Return the selected model for token-spending contract tests."""
    return str(pytestconfig.getoption("contract_model"))


@pytest.fixture(scope="session")
def require_cli(pytestconfig: pytest.Config) -> bool:
    """Return whether missing installed console scripts are test failures."""
    return bool(pytestconfig.getoption("require_cli"))


@pytest.fixture(scope="session")
def require_pi_package_smoke(pytestconfig: pytest.Config) -> bool:
    """Return whether nightly Pi package smoke evidence is required."""
    return bool(pytestconfig.getoption("require_pi_package_smoke"))


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
    if request.module.__name__.endswith(("agents.test_runtime", "agents.test_execution_policy")):
        return
    if request.node.get_closest_marker("contract"):
        return

    def _stub_is_agent_authenticated(
        _agent: str,
        *,
        auth_status_timeout: int = 10,
        pi_dir: Path | None = None,
    ) -> bool:
        del auth_status_timeout, pi_dir
        return True

    monkeypatch.setattr(
        "hephaestus.agents.runtime.is_agent_authenticated",
        _stub_is_agent_authenticated,
    )

    def _stub_resolve_agent(
        agent: str | None,
        *,
        cwd: Path | None = None,
        disable_pi_automation: bool = False,
        auth_status_timeout: int = 10,
        pi_isolation_adapter: str | None = None,
        pi_dir: Path | None = None,
    ) -> str:
        del cwd, disable_pi_automation, auth_status_timeout, pi_isolation_adapter, pi_dir
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
