"""Tests for hephaestus.constants path helpers and shared constants."""

from __future__ import annotations

from pathlib import Path

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


def test_repo_root_ignores_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repository discovery is filesystem-only even when the legacy variable is set."""
    (tmp_path / "pyproject.toml").write_text("")
    monkeypatch.setenv("HEPHAESTUS_REPO_ROOT", str(tmp_path))
    root = constants.repo_root()
    assert (root / "pyproject.toml").is_file()
    assert root != tmp_path


def test_scripts_dir_matches_repo_root() -> None:
    """scripts_dir() returns repo_root() / 'scripts'."""
    assert constants.scripts_dir() == constants.repo_root() / "scripts"
    assert constants.scripts_dir().is_dir()


@pytest.mark.parametrize(
    ("constant_name", "value"),
    [
        ("AGENT_IMPL_TIMEOUT", 1800),
        ("AGENT_REVIEW_TIMEOUT", 1200),
        ("AGENT_PLAN_TIMEOUT", 1200),
        ("AGENT_LEARN_TIMEOUT", 1200),
        ("AGENT_GIT_TIMEOUT", 30),
        ("AGENT_CLONE_TIMEOUT", 120),
        ("AGENT_AUTH_STATUS_TIMEOUT", 10),
        ("AGENT_REBASE_TIMEOUT", 2400),
        ("DIFF_COLLECT_TIMEOUT", 60),
        ("PRE_PR_TEST_TIMEOUT", 600),
    ],
)
def test_agent_timeout_defaults_are_importable(constant_name: str, value: int) -> None:
    """Agent timeout defaults are named constants in the library layer."""
    assert getattr(constants, constant_name) == value


def test_timeout_environment_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy timeout variables cannot alter the fixed library defaults."""
    monkeypatch.setenv("HEPH_AGENT_GIT_TIMEOUT", "999")
    assert constants.agent_git_timeout() == constants.AGENT_GIT_TIMEOUT == 30


def test_read_timeout_env_api_is_removed() -> None:
    """The arbitrary environment-variable reader is no longer public code."""
    assert not hasattr(constants, "read_timeout_env")
