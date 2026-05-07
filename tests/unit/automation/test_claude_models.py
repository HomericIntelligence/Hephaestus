"""Tests for hephaestus.automation.claude_models phase-to-model routing."""

from __future__ import annotations

import importlib

import pytest

from hephaestus.automation import claude_models


class TestDefaults:
    """Default mapping reflects the cost/quality tradeoff per phase."""

    def test_planner_defaults_to_opus(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_PLANNER_MODEL", raising=False)
        assert claude_models.planner_model() == claude_models.OPUS

    def test_implementer_defaults_to_haiku(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_IMPLEMENTER_MODEL", raising=False)
        assert claude_models.implementer_model() == claude_models.HAIKU

    def test_reviewer_defaults_to_sonnet(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_REVIEWER_MODEL", raising=False)
        assert claude_models.reviewer_model() == claude_models.SONNET


class TestEnvOverride:
    """An operator can flip a phase's model without code changes.

    Useful when one tier's quota is exhausted (the original bug —
    Opus quota ran out, blocking every implementer call until the user
    could pin Haiku).
    """

    def test_planner_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_PLANNER_MODEL", "claude-haiku-4-5")
        assert claude_models.planner_model() == "claude-haiku-4-5"

    def test_implementer_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "claude-opus-4-7")
        assert claude_models.implementer_model() == "claude-opus-4-7"


class TestModuleStable:
    """Module reimport stability guard.

    Reimporting the module shouldn't change defaults — guards against
    accidental top-level ``os.environ.get()`` reads being cached.
    """

    def test_reimport_idempotent(self) -> None:
        importlib.reload(claude_models)
        assert claude_models.OPUS == "claude-opus-4-7"
        assert claude_models.HAIKU == "claude-haiku-4-5"
        assert claude_models.SONNET == "claude-sonnet-4-6"
