"""Tests for explicit pytest controls used by CI and opt-in test lanes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from tests import conftest as root_conftest
from tests.integration import test_cli_entry_points

REPO_ROOT = Path(__file__).resolve().parents[3]
EXPECTED_OPTIONS = (
    "--run-contract-tests",
    "--run-contract-agent",
    "--contract-repo",
    "--contract-model",
    "--require-cli",
    "--require-pi-package-smoke",
)
REMOVED_ENV_NAMES = (
    "HEPHAESTUS_CONTRACT_TESTS",
    "HEPHAESTUS_CONTRACT_AGENT",
    "HEPHAESTUS_CONTRACT_REPO",
    "HEPHAESTUS_CONTRACT_MODEL",
    "HEPHAESTUS_REQUIRE_CLI",
    "HEPHAESTUS_REQUIRE_PI_PACKAGE_SMOKE",
)
OWNED_EXECUTION_PATHS = (
    REPO_ROOT / "tests" / "conftest.py",
    REPO_ROOT / "tests" / "integration",
    REPO_ROOT / "tests" / "unit" / "ci",
    REPO_ROOT / "Justfile",
    REPO_ROOT / ".github" / "workflows",
    REPO_ROOT / "scripts" / "run_ci_local.sh",
)


def _owned_files() -> list[Path]:
    """Return text files in the environment-control migration boundary."""
    files: list[Path] = []
    for path in OWNED_EXECUTION_PATHS:
        if path.is_file():
            files.append(path)
        else:
            files.extend(
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and "__pycache__" not in candidate.parts
            )
    return [path for path in files if path.resolve() != Path(__file__).resolve()]


def test_pytest_help_advertises_explicit_control_options() -> None:
    """The test controls are discoverable through pytest's public CLI."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--help"],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    for option in EXPECTED_OPTIONS:
        assert option in result.stdout


def test_legacy_environment_cannot_enable_contract_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned legacy environment cannot bypass the explicit CLI gate."""

    class Config:
        def getoption(self, name: str) -> bool:
            assert name == "run_contract_tests"
            return False

    class Item:
        def __init__(self) -> None:
            self.markers: list[Any] = []

        def get_closest_marker(self, name: str) -> object | None:
            return object() if name == "contract" else None

        def add_marker(self, marker: Any) -> None:
            self.markers.append(marker)

    for name in REMOVED_ENV_NAMES:
        monkeypatch.setenv(name, "poison")
    monkeypatch.setenv("HEPHAESTUS_CONTRACT_TESTS", "1")
    item = Item()

    root_conftest.pytest_collection_modifyitems(
        cast(pytest.Config, Config()),
        [cast(pytest.Item, item)],
    )

    assert item.markers, "contract test was not skipped without --run-contract-tests"


def test_legacy_environment_cannot_require_installed_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poisoned legacy environment cannot turn a default skip into failure."""
    monkeypatch.setenv("HEPHAESTUS_REQUIRE_CLI", "1")

    with pytest.raises(pytest.skip.Exception):
        test_cli_entry_points._resolve_binary(
            "hephaestus-definitely-not-a-real-binary",
            required=False,
        )


def test_owned_execution_surfaces_do_not_reference_removed_environment_controls() -> None:
    """Legacy environment controls cannot be reintroduced in migrated files."""
    violations: list[str] = []
    for path in _owned_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in REMOVED_ENV_NAMES:
            if name in text:
                violations.append(f"{path.relative_to(REPO_ROOT)}: {name}")

    assert not violations, "legacy environment controls found:\n" + "\n".join(violations)
