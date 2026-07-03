"""Tests for hephaestus.automation.claude_models phase-to-model routing."""

from __future__ import annotations

import importlib

import pytest

from hephaestus.automation import agent_config as claude_models


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

    def test_codex_advise_defaults_to_gpt_mini(self) -> None:
        assert claude_models.codex_advise_model() == "gpt-5.4-mini"

    def test_git_message_defaults_to_haiku(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_GIT_MESSAGE_MODEL", raising=False)
        assert claude_models.git_message_model() == claude_models.HAIKU


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

    def test_git_message_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HEPH_GIT_MESSAGE_MODEL", "claude-fable-5")
        assert claude_models.git_message_model() == "claude-fable-5"


class TestModuleStable:
    """Module reimport stability guard.

    Reimporting the module shouldn't change defaults — guards against
    accidental top-level ``os.environ.get()`` reads being cached.
    """

    def test_reimport_idempotent(self) -> None:
        importlib.reload(claude_models)
        # #1781 made Opus 4.8 the production default; this branch carries the
        # matching test expectation so full-suite validation stays green.
        assert claude_models.OPUS == "claude-opus-4-8"
        assert claude_models.HAIKU == "claude-haiku-4-5"
        assert claude_models.SONNET == "claude-sonnet-4-6"
        assert claude_models.CODEX_ADVISE == "gpt-5.4-mini"


class TestEnvVarValidation:
    """A5-04: unknown env-var overrides warn but do not crash."""

    def test_known_override_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known model IDs produce no warning."""
        import logging

        monkeypatch.setenv("HEPH_PLANNER_MODEL", claude_models.HAIKU)
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.planner_model()
        assert result == claude_models.HAIKU
        assert not caplog.records

    def test_unknown_override_warns_but_returns_value(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unknown model IDs trigger a warning but are still returned (A5-04)."""
        import logging

        monkeypatch.setenv("HEPH_IMPLEMENTER_MODEL", "claude-preview-99-99")
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
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
            "HEPH_GIT_MESSAGE_MODEL",
        ):
            monkeypatch.setenv(env_var, model_id)

        assert claude_models.planner_model() == model_id
        assert claude_models.implementer_model() == model_id
        assert claude_models.reviewer_model() == model_id
        assert claude_models.advise_model() == model_id
        assert claude_models.learn_model() == model_id
        assert claude_models.git_message_model() == model_id


class TestNewerModelsRecognized:
    """Newer models are recognized — no spurious 'Unknown model' warning.

    ``claude-opus-4-8`` and ``claude-fable-5`` are valid IDs (the Fable tier
    sits above Opus). An operator pinning them must not be nagged every call.
    """

    @pytest.mark.parametrize("model_id", ["claude-opus-4-8", "claude-fable-5"])
    def test_newer_model_override_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        model_id: str,
    ) -> None:
        import logging

        monkeypatch.setenv("HEPH_REVIEWER_MODEL", model_id)
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.reviewer_model()
        assert result == model_id
        assert not caplog.records

    def test_newer_models_in_known_set(self) -> None:
        assert "claude-opus-4-8" in claude_models._KNOWN_MODELS
        assert "claude-fable-5" in claude_models._KNOWN_MODELS

    def test_genuinely_unknown_model_still_warns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Adding newer models must not suppress warnings for true typos."""
        import logging

        monkeypatch.setenv("HEPH_REVIEWER_MODEL", "claude-fbale-5")  # typo
        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.reviewer_model()
        assert result == "claude-fbale-5"
        assert any("Unknown model" in r.message for r in caplog.records)
