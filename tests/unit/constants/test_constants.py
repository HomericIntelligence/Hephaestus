"""Tests for hephaestus.constants path helpers and shared constants."""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from types import ModuleType

import pytest

from hephaestus import constants
from hephaestus.constants import TRANSIENT_ERROR_CORE
from hephaestus.resilience.subprocess_resilience import TRANSIENT_ERROR_PATTERNS
from hephaestus.utils.retry import NETWORK_ERROR_KEYWORDS

# Signals that are genuinely shared by both the resilience and retry layers.
# These MUST stay present in both consumer lists; the canonical core exists so
# they cannot drift apart (issue #1205).
SHARED_TRANSIENT_SIGNALS = (
    "connection",
    "timed out",
    "temporary failure",
    "could not resolve",
    "503",
    "502",
    "504",
)

AGENT_TIMEOUT_CONSTANTS = (
    ("AGENT_AUTH_STATUS_TIMEOUT", "HEPH_AGENT_AUTH_STATUS_TIMEOUT", 10),
    ("AGENT_GIT_TIMEOUT", "HEPH_AGENT_GIT_TIMEOUT", 30),
    ("AGENT_DIFF_TIMEOUT", "HEPH_AGENT_DIFF_TIMEOUT", 60),
    ("AGENT_CLONE_TIMEOUT", "HEPH_AGENT_CLONE_TIMEOUT", 120),
    ("AGENT_DEFAULT_TIMEOUT", "HEPH_AGENT_DEFAULT_TIMEOUT", 300),
    ("AGENT_PLAN_TIMEOUT", "HEPH_AGENT_PLAN_TIMEOUT", 300),
    ("AGENT_LEARN_TIMEOUT", "HEPH_AGENT_LEARN_TIMEOUT", 300),
    ("AGENT_REVIEW_TIMEOUT", "HEPH_AGENT_REVIEW_TIMEOUT", 600),
    ("AGENT_PRE_PR_TEST_TIMEOUT", "HEPH_AGENT_PRE_PR_TEST_TIMEOUT", 600),
    ("AGENT_IMPL_TIMEOUT", "HEPH_AGENT_IMPL_TIMEOUT", 1800),
    ("AGENT_REBASE_TIMEOUT", "HEPH_AGENT_REBASE_TIMEOUT", 2400),
)


def _reload_constants_module() -> ModuleType:
    return importlib.reload(constants)


class TestAgentTimeoutConstants:
    """Tests for canonical agent subprocess timeout constants."""

    @pytest.mark.parametrize(("constant_name", "_env_name", "default"), AGENT_TIMEOUT_CONSTANTS)
    def test_defaults(self, constant_name: str, _env_name: str, default: int) -> None:
        """Agent timeout constants expose the documented default values."""
        assert getattr(constants, constant_name) == default

    @pytest.mark.parametrize(("constant_name", "env_name", "_default"), AGENT_TIMEOUT_CONSTANTS)
    def test_env_overrides(
        self,
        monkeypatch: pytest.MonkeyPatch,
        constant_name: str,
        env_name: str,
        _default: int,
    ) -> None:
        """Each timeout can be tuned through its own HEPH_* env var."""
        monkeypatch.setenv(env_name, "77")
        try:
            reloaded = _reload_constants_module()
            assert getattr(reloaded, constant_name) == 77
        finally:
            monkeypatch.delenv(env_name, raising=False)
            _reload_constants_module()

    def test_invalid_env_logs_and_uses_default(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Malformed timeout overrides warn and fall back to the default."""
        monkeypatch.setenv("HEPH_AGENT_IMPL_TIMEOUT", "slow")

        try:
            with caplog.at_level(logging.WARNING, logger="hephaestus.constants"):
                reloaded = _reload_constants_module()
        finally:
            monkeypatch.delenv("HEPH_AGENT_IMPL_TIMEOUT", raising=False)
            _reload_constants_module()

        assert reloaded.AGENT_IMPL_TIMEOUT == 1800
        assert any("HEPH_AGENT_IMPL_TIMEOUT" in record.message for record in caplog.records)


class TestTransientErrorCore:
    """Tests for the canonical TRANSIENT_ERROR_CORE shared by two consumers."""

    def test_is_frozenset(self) -> None:
        """TRANSIENT_ERROR_CORE must be a frozenset, not a mutable set."""
        assert isinstance(TRANSIENT_ERROR_CORE, frozenset)

    def test_all_entries_are_strings(self) -> None:
        """Every entry in the core is a string."""
        for entry in TRANSIENT_ERROR_CORE:
            assert isinstance(entry, str)

    def test_all_entries_are_lowercase(self) -> None:
        """All entries are lowercase for case-insensitive substring matching."""
        for entry in TRANSIENT_ERROR_CORE:
            assert entry == entry.lower(), f"not lowercase: {entry}"

    @pytest.mark.parametrize("substring", SHARED_TRANSIENT_SIGNALS)
    def test_core_contains_shared_signal(self, substring: str) -> None:
        """Each genuinely-shared transient signal lives in the canonical core."""
        assert substring in TRANSIENT_ERROR_CORE

    def test_immutability(self) -> None:
        """Frozenset should reject mutation attempts."""
        with pytest.raises(AttributeError):
            TRANSIENT_ERROR_CORE.add("nope")  # type: ignore[attr-defined]
        with pytest.raises(AttributeError):
            TRANSIENT_ERROR_CORE.discard("connection")  # type: ignore[attr-defined]

    @pytest.mark.parametrize("substring", SHARED_TRANSIENT_SIGNALS)
    def test_shared_signal_present_in_subprocess_patterns(self, substring: str) -> None:
        """Anti-drift: every shared signal is reachable from the subprocess list."""
        assert any(substring in pattern for pattern in TRANSIENT_ERROR_PATTERNS)

    @pytest.mark.parametrize("substring", SHARED_TRANSIENT_SIGNALS)
    def test_shared_signal_present_in_network_keywords(self, substring: str) -> None:
        """Anti-drift: every shared signal is reachable from the network list."""
        assert any(substring in keyword for keyword in NETWORK_ERROR_KEYWORDS)


def test_repo_root_resolves_to_repo_containing_pyproject() -> None:
    """repo_root() finds the directory containing pyproject.toml."""
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()
    assert (root / "hephaestus").is_dir()


def test_repo_root_honors_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """repo_root() uses HEPHAESTUS_REPO_ROOT env var when it contains pyproject.toml."""
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", str(tmp_path))
    assert constants.repo_root() == tmp_path


def test_repo_root_ignores_env_without_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """repo_root() falls back to walk-up if env var path lacks pyproject.toml."""
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", str(tmp_path))  # no pyproject.toml
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()


def test_repo_root_ignores_nonexistent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """repo_root() falls back to walk-up if env var path does not exist."""
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", "/nonexistent/path/xyz")
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()


def test_scripts_dir_matches_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts_dir() returns repo_root() / 'scripts'."""
    monkeypatch.delenv("HEPHAESTUS_REPO_ROOT", raising=False)
    assert constants.scripts_dir() == constants.repo_root() / "scripts"
    assert constants.scripts_dir().is_dir()
