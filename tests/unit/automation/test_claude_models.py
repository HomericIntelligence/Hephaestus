"""Tests for hephaestus.automation.claude_models phase-to-model routing."""

from __future__ import annotations

import importlib

import pytest

from hephaestus.automation import agent_config as claude_models


class TestDefaults:
    """Omitted model settings use the selected tool default."""

    def test_planner_uses_tool_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_PLANNER_MODEL", raising=False)
        assert claude_models.planner_model() == ""

    def test_implementer_uses_tool_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_IMPLEMENTER_MODEL", raising=False)
        assert claude_models.implementer_model() == ""

    def test_reviewer_uses_tool_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HEPH_REVIEWER_MODEL", raising=False)
        assert claude_models.reviewer_model() == ""

    def test_fallback_requires_explicit_selection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The fallback requires an explicit model."""
        monkeypatch.delenv("HEPH_FALLBACK_MODEL", raising=False)
        assert claude_models.fallback_model() == ""


class TestExplicitOverride:
    """An operator can flip a phase's model through typed CLI configuration.

    Useful when one tier's quota is exhausted (the original bug —
    Opus quota ran out, blocking every implementer call until the user
    could pin Haiku).
    """

    def test_planner_override(self) -> None:
        assert claude_models.planner_model("claude-haiku-4-5") == "claude-haiku-4-5"

    def test_implementer_override(self) -> None:
        assert claude_models.implementer_model("claude-opus-4-7") == "claude-opus-4-7"

    def test_fallback_override(self) -> None:
        assert claude_models.fallback_model("claude-sonnet-4-6") == "claude-sonnet-4-6"

    def test_fallback_unknown_override_returns_value_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.fallback_model("claude-preview-99-99")
        assert result == "claude-preview-99-99"
        assert not caplog.records


class TestModuleStable:
    """Module reimport stability guard.

    Reimporting the module shouldn't change defaults — guards against
    accidental top-level ``os.environ.get()`` reads being cached.
    """

    def test_reimport_idempotent(self) -> None:
        expected = (
            claude_models.planner_model(),
            claude_models.implementer_model(),
            claude_models.reviewer_model(),
        )
        importlib.reload(claude_models)
        assert expected == (
            claude_models.planner_model(),
            claude_models.implementer_model(),
            claude_models.reviewer_model(),
        )


class TestExplicitValueValidation:
    """Explicit model strings pass through without catalog warnings."""

    def test_known_override_no_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Known model IDs produce no warning."""
        import logging

        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.planner_model("MyPrivateModel")
        assert result == "MyPrivateModel"
        assert not caplog.records

    def test_unknown_override_returns_value_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Arbitrary model names pass through without a warning."""
        import logging

        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.implementer_model("claude-preview-99-99")
        assert result == "claude-preview-99-99"
        assert not caplog.records

    def test_all_phase_functions_accept_unknown_model(
        self,
    ) -> None:
        """All phase functions accept overrides without raising (A5-04)."""
        model_id = "claude-experimental-0-0"
        assert claude_models.planner_model(model_id) == model_id
        assert claude_models.implementer_model(model_id) == model_id
        assert claude_models.reviewer_model(model_id) == model_id
        assert claude_models.advise_model(model_id) == model_id
        assert claude_models.learn_model(model_id) == model_id


class TestNewerModelsRecognized:
    """Model IDs and former aliases pass through as literal strings."""

    @pytest.mark.parametrize(
        ("raw_model", "expected_model"),
        [
            ("claude-opus-4-8", "claude-opus-4-8"),
            ("claude-fable-5", "claude-fable-5"),
            ("fable", "fable"),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("claude-mythos-5", "claude-mythos-5"),
            ("mythos", "mythos"),
        ],
    )
    def test_newer_model_override_no_warning_and_preserves_names(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        raw_model: str,
        expected_model: str,
    ) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.reviewer_model(raw_model)
        assert result == expected_model
        assert not caplog.records

    @pytest.mark.parametrize(
        ("raw_model", "expected_model"),
        [
            ("", ""),
            (" fable ", "fable"),
            ("fable:future-effort", "fable"),
            ("MYTHOS", "MYTHOS"),
            ("claude-sonnet-5", "claude-sonnet-5"),
            ("claude-sonnet-5:future-effort", "claude-sonnet-5"),
            ("claude-preview-99-99", "claude-preview-99-99"),
        ],
    )
    def test_normalize_claude_model(self, raw_model: str, expected_model: str) -> None:
        assert claude_models.normalize_claude_model(raw_model) == expected_model

    def test_genuinely_unknown_model_does_not_warn(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Model spelling is the provider responsibility."""
        import logging

        with caplog.at_level(logging.WARNING, logger="hephaestus.automation.agent_config"):
            result = claude_models.reviewer_model("claude-fbale-5")  # typo
        assert result == "claude-fbale-5"
        assert not caplog.records
