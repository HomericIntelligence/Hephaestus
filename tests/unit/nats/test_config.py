"""Tests for hephaestus.nats.config."""

from __future__ import annotations

import pytest

from hephaestus.nats.config import NATSConfig, load_nats_config


class TestNATSConfig:
    """Tests for NATSConfig model."""

    def test_defaults(self) -> None:
        config = NATSConfig()
        assert config.enabled is False
        assert config.url == "nats://localhost:4222"
        assert config.stream == "TASKS"
        assert config.subjects == []
        assert config.durable_name == "hephaestus-subscriber"
        assert config.deliver_policy == "new"

    def test_custom_values(self) -> None:
        config = NATSConfig(
            enabled=True,
            url="nats://remote:4222",
            stream="EVENTS",
            subjects=["my.subject.>"],
            durable_name="my-consumer",
            deliver_policy="all",
        )
        assert config.enabled is True
        assert config.url == "nats://remote:4222"
        assert config.subjects == ["my.subject.>"]

    def test_invalid_extra_field_ignored(self) -> None:
        config = NATSConfig(enabled=True)
        assert config.enabled is True


class TestLoadNATSConfig:
    """Tests for load_nats_config()."""

    def test_loads_from_dict(self) -> None:
        config = load_nats_config({"enabled": True, "url": "nats://test:4222"})
        assert config.enabled is True
        assert config.url == "nats://test:4222"

    def test_env_override_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "nats://env-override:4222")
        config = load_nats_config({})
        assert config.url == "nats://env-override:4222"

    def test_env_override_stream(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_STREAM", "MY_STREAM")
        config = load_nats_config({})
        assert config.stream == "MY_STREAM"

    def test_env_override_durable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_DURABLE_NAME", "my-durable")
        config = load_nats_config({})
        assert config.durable_name == "my-durable"

    def test_no_env_override_when_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "nats://should-be-ignored:4222")
        config = load_nats_config({"url": "nats://original:4222"}, env_override=False)
        assert config.url == "nats://original:4222"

    def test_empty_dict_uses_defaults(self) -> None:
        config = load_nats_config({})
        assert config.enabled is False
        assert config.durable_name == "hephaestus-subscriber"
