"""Tests for hephaestus.nats.config."""

from __future__ import annotations

import ssl

import pytest

from hephaestus.nats.config import NATSConfig, load_nats_config


class TestNATSConfig:
    """Tests for NATSConfig model."""

    def test_defaults(self) -> None:
        config = NATSConfig()
        assert config.enabled is False
        assert config.url == "tls://localhost:4222"
        assert config.tls is True
        assert config.tls_ca_file is None
        assert config.tls_cert_file is None
        assert config.tls_key_file is None
        assert config.tls_hostname is None
        assert config.tls_handshake_first is False
        assert config.allow_plaintext is False
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
        config = load_nats_config({"enabled": True, "unknown_key": "ignored"})
        assert config.enabled is True

    def test_backoff_defaults_preserve_historical_constants(self) -> None:
        config = NATSConfig()
        assert config.initial_backoff_seconds == 1.0
        assert config.max_backoff_seconds == 60.0
        assert config.backoff_multiplier == 2.0

    def test_initial_backoff_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            NATSConfig(initial_backoff_seconds=0.0)

    def test_max_backoff_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            NATSConfig(max_backoff_seconds=-1.0)

    def test_backoff_multiplier_must_exceed_one(self) -> None:
        with pytest.raises(ValueError):
            NATSConfig(backoff_multiplier=1.0)

    def test_max_below_initial_rejected(self) -> None:
        with pytest.raises(ValueError):
            NATSConfig(initial_backoff_seconds=10.0, max_backoff_seconds=5.0)

    def test_nonlocal_plaintext_rejected_when_enabled(self) -> None:
        with pytest.raises(ValueError, match="plaintext nats://"):
            NATSConfig(enabled=True, url="nats://broker.example.com:4222", tls=False)

    def test_nonlocal_plaintext_ws_rejected_when_enabled(self) -> None:
        with pytest.raises(ValueError, match="plaintext ws://"):
            NATSConfig(enabled=True, url="ws://broker.example.com:4222", tls=False)

    def test_loopback_plaintext_allowed_for_local_development(self) -> None:
        config = NATSConfig(enabled=True, url="nats://127.0.0.1:4222", tls=False)
        assert config.tls_enabled is False

    def test_explicit_allow_plaintext_permits_nonlocal_nats_url(self) -> None:
        config = NATSConfig(
            enabled=True,
            url="nats://broker.example.com:4222",
            tls=False,
            allow_plaintext=True,
        )
        assert config.allow_plaintext is True
        assert config.tls_enabled is False

    def test_tls_key_requires_tls_cert(self) -> None:
        with pytest.raises(ValueError, match="tls_key_file requires tls_cert_file"):
            NATSConfig(tls_key_file="/run/secrets/nats.key")

    def test_tls_scheme_enables_tls_options_even_when_flag_is_false(self) -> None:
        config = NATSConfig(url="tls://broker.example.com:4222", tls=False)
        assert config.tls_enabled is True

    def test_connect_options_include_ssl_context_and_tls_kwargs(self) -> None:
        config = NATSConfig(
            url="tls://broker.example.com:4222",
            tls=True,
            tls_hostname="broker.example.com",
            tls_handshake_first=True,
        )

        options = config.connect_options()

        assert isinstance(options["tls"], ssl.SSLContext)
        assert options["tls_hostname"] == "broker.example.com"
        assert options["tls_handshake_first"] is True

    def test_connect_options_empty_for_local_plaintext(self) -> None:
        config = NATSConfig(enabled=True, url="nats://localhost:4222", tls=False)
        assert config.connect_options() == {}


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

    def test_env_override_initial_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_INITIAL_BACKOFF_SECONDS", "0.5")
        config = load_nats_config({})
        assert config.initial_backoff_seconds == 0.5

    def test_env_override_max_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_MAX_BACKOFF_SECONDS", "120.0")
        config = load_nats_config({})
        assert config.max_backoff_seconds == 120.0

    def test_env_override_backoff_multiplier(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_BACKOFF_MULTIPLIER", "3.0")
        config = load_nats_config({})
        assert config.backoff_multiplier == 3.0

    def test_env_override_invalid_float_names_variable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NATS_INITIAL_BACKOFF_SECONDS", "not-a-number")
        with pytest.raises(ValueError, match="NATS_INITIAL_BACKOFF_SECONDS"):
            load_nats_config({})

    def test_env_override_disabled_ignores_backoff_vars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NATS_INITIAL_BACKOFF_SECONDS", "9.9")
        config = load_nats_config({}, env_override=False)
        assert config.initial_backoff_seconds == 1.0

    def test_extra_yaml_keys_ignored(self) -> None:
        # Regression for issue #1458: NATSConfig moved from pydantic (which
        # silently ignored extras) to a stdlib dataclass (which raises on
        # unknown kwargs). load_nats_config must keep dropping unknown keys.
        config = load_nats_config({"url": "nats://x:4222", "unknown_key": "ignored"})
        assert config.url == "nats://x:4222"

    def test_yaml_string_backoff_coerced_to_float(self) -> None:
        config = load_nats_config({"initial_backoff_seconds": "0.5"})
        assert config.initial_backoff_seconds == 0.5

    def test_yaml_string_false_does_not_permit_nonlocal_plaintext(self) -> None:
        with pytest.raises(ValueError, match="plaintext nats://"):
            load_nats_config(
                {
                    "enabled": True,
                    "url": "nats://broker.example.com:4222",
                    "tls": "false",
                    "allow_plaintext": "false",
                },
                env_override=False,
            )

    def test_yaml_string_bools_are_coerced_before_validation(self) -> None:
        config = load_nats_config(
            {
                "enabled": True,
                "url": "tls://broker.example.com:4222",
                "tls": "false",
                "tls_handshake_first": "true",
                "allow_plaintext": "false",
            },
            env_override=False,
        )

        assert config.tls is False
        assert config.tls_handshake_first is True
        assert config.allow_plaintext is False

    def test_yaml_invalid_bool_names_field(self) -> None:
        with pytest.raises(ValueError, match="allow_plaintext"):
            load_nats_config({"allow_plaintext": "sometimes"}, env_override=False)

    def test_env_reads_tls_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "tls://broker.example.com:4222")
        monkeypatch.setenv("NATS_TLS", "true")
        monkeypatch.setenv("NATS_TLS_CA_FILE", "/run/secrets/nats-ca.pem")
        monkeypatch.setenv("NATS_TLS_CERT_FILE", "/run/secrets/nats-client.pem")
        monkeypatch.setenv("NATS_TLS_KEY_FILE", "/run/secrets/nats-client.key")
        monkeypatch.setenv("NATS_TLS_HOSTNAME", "broker.example.com")
        monkeypatch.setenv("NATS_TLS_HANDSHAKE_FIRST", "yes")

        config = load_nats_config({"enabled": True})

        assert config.url == "tls://broker.example.com:4222"
        assert config.tls is True
        assert config.tls_ca_file == "/run/secrets/nats-ca.pem"
        assert config.tls_cert_file == "/run/secrets/nats-client.pem"
        assert config.tls_key_file == "/run/secrets/nats-client.key"
        assert config.tls_hostname == "broker.example.com"
        assert config.tls_handshake_first is True

    def test_env_false_bool_permits_local_plaintext(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "nats://127.0.0.1:4222")
        monkeypatch.setenv("NATS_TLS", "false")

        config = load_nats_config({"enabled": True})

        assert config.tls is False
        assert config.tls_enabled is False

    def test_invalid_bool_env_names_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_TLS", "sometimes")
        with pytest.raises(ValueError, match="NATS_TLS"):
            load_nats_config({})


class TestFromEnv:
    """Tests for NATSConfig.from_env()."""

    def test_no_env_uses_defaults(self) -> None:
        config = NATSConfig.from_env()
        assert config.url == "tls://localhost:4222"
        assert config.stream == "TASKS"
        assert config.initial_backoff_seconds == 1.0

    def test_reads_string_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "nats://env:4222")
        monkeypatch.setenv("NATS_STREAM", "EVENTS")
        monkeypatch.setenv("NATS_DURABLE_NAME", "env-durable")
        config = NATSConfig.from_env()
        assert config.url == "nats://env:4222"
        assert config.stream == "EVENTS"
        assert config.durable_name == "env-durable"

    def test_reads_numeric_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_INITIAL_BACKOFF_SECONDS", "0.5")
        monkeypatch.setenv("NATS_MAX_BACKOFF_SECONDS", "30")
        monkeypatch.setenv("NATS_BACKOFF_MULTIPLIER", "1.5")
        config = NATSConfig.from_env()
        assert config.initial_backoff_seconds == 0.5
        assert config.max_backoff_seconds == 30.0
        assert config.backoff_multiplier == 1.5

    def test_overrides_kwargs_are_base(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "nats://env-wins:4222")
        config = NATSConfig.from_env(enabled=True, url="nats://kwarg:4222")
        assert config.enabled is True  # kwarg with no env var survives
        assert config.url == "nats://env-wins:4222"  # env overrides kwarg

    def test_invalid_numeric_names_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_BACKOFF_MULTIPLIER", "abc")
        with pytest.raises(ValueError, match="NATS_BACKOFF_MULTIPLIER"):
            NATSConfig.from_env()

    def test_invalid_backoff_bounds_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_INITIAL_BACKOFF_SECONDS", "10")
        monkeypatch.setenv("NATS_MAX_BACKOFF_SECONDS", "5")
        with pytest.raises(ValueError, match="max_backoff_seconds"):
            NATSConfig.from_env()

    def test_empty_string_var_ignored(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("NATS_URL", "")
        config = NATSConfig.from_env()
        assert config.url == "tls://localhost:4222"
