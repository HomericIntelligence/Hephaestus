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


class TestEnvVarValidation:
    """A5-04: unknown env-var overrides warn but do not crash."""

    def test_known_override_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known model IDs produce no warning."""
        import logging

        monkeypatch.setenv("HEPH_PLANNER_MODEL", claude_models.HAIKU)
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_models"):
            result = claude_models.planner_model()
        assert result == claude_models.HAIKU
        assert not caplog.records

    def test_unknown_override_warns_but_returns_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown model IDs trigger a warning but are still returned (A5-04)."""
        import logging

        monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "claude-preview-99-99")
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.claude_models"):
            result = claude_models.implementer_model()
        assert result == "claude-preview-99-99"
        assert any("Unknown model" in r.message for r in caplog.records)

    def test_all_phase_functions_accept_unknown_model(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All phase functions accept overrides without raising (A5-04)."""
        model_id = "claude-experimental-0-0"
        for env_var in (
            "HEPH_PLANNER_MODEL",
            "HEPH_IMPLEMENTER_MODEL",
            "HEPH_REVIEWER_MODEL",
            "HEPH_ADVISE_MODEL",
            "HEPH_LEARN_MODEL",
        ):
            monkeypatch.setenv(env_var, model_id)

        assert claude_models.planner_model() == model_id
        assert claude_models.implementer_model() == model_id
        assert claude_models.reviewer_model() == model_id
        assert claude_models.advise_model() == model_id
        assert claude_models.learn_model() == model_id
