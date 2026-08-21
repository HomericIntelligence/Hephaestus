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

    def test_empty_dict_uses_defaults(self) -> None:
        config = load_nats_config({})
        assert config.enabled is False
        assert config.durable_name == "hephaestus-subscriber"

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("NATS_URL", "nats://poison:4222"),
            ("NATS_STREAM", "POISON"),
            ("NATS_DURABLE_NAME", "poison"),
            ("NATS_INITIAL_BACKOFF_SECONDS", "9.9"),
            ("NATS_MAX_BACKOFF_SECONDS", "999"),
            ("NATS_BACKOFF_MULTIPLIER", "4"),
            ("NATS_TLS", "false"),
            ("NATS_TLS_CA_FILE", "/poison/ca"),
            ("NATS_TLS_CERT_FILE", "/poison/cert"),
            ("NATS_TLS_KEY_FILE", "/poison/key"),
            ("NATS_TLS_HOSTNAME", "poison.example"),
            ("NATS_TLS_HANDSHAKE_FIRST", "true"),
        ],
    )
    def test_retired_environment_variables_are_ignored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        name: str,
        value: str,
    ) -> None:
        monkeypatch.setenv(name, value)
        config = load_nats_config({})

        assert config.url == "tls://localhost:4222"
        assert config.stream == "TASKS"
        assert config.durable_name == "hephaestus-subscriber"
        assert config.initial_backoff_seconds == 1.0
        assert config.max_backoff_seconds == 60.0
        assert config.backoff_multiplier == 2.0
        assert config.tls is True
        assert config.tls_ca_file is None
        assert config.tls_cert_file is None
        assert config.tls_key_file is None
        assert config.tls_hostname is None
        assert config.tls_handshake_first is False

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
        )

        assert config.tls is False
        assert config.tls_handshake_first is True
        assert config.allow_plaintext is False

    def test_yaml_invalid_bool_names_field(self) -> None:
        with pytest.raises(ValueError, match="allow_plaintext"):
            load_nats_config({"allow_plaintext": "sometimes"})

    def test_environment_derived_constructor_was_removed(self) -> None:
        assert not hasattr(NATSConfig, "from_env")
